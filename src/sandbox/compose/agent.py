"""The agent container: the override fragment that adds a hardened ``agent`` service.

Every set gets one. It runs the bundled default image (``sandbox/images/agent``, built by
Compose on first use and tagged with a hash of its build context, so an updated image is
rebuilt) as the developer's
host UID:GID, never root, with every capability dropped, ``no-new-privileges``, and no
Docker socket. It sees exactly:

- the set directory at ``/workspace/<set>/`` (where shells start), its plan
  ``sandbox.yaml`` read-only (the plan pins the agent override);
- each source repo's ``.git`` read-write at its exact host path, because a worktree's
  ``.git`` file points there; the source's working tree itself is never mounted;
- the host's ``~/.claude/`` at ``/home/agent/.claude`` (``CLAUDE_CONFIG_DIR``), so every
  set shares one login. The host's ``~/.claude.json`` is not mounted;
- in a set with a web app, the mkcert CA's ``rootCA.pem`` (never its key) read-only at
  ``/sandbox/ca/rootCA.pem``. The bundled image's entrypoint adds it to the system trust
  store, and ``NODE_EXTRA_CA_CERTS`` points Node (``claude``) at it.

An **agent override** (a Compose fragment from the set plan, pinned there from a template
or the global config) may replace the image, add settings to the agent and add services
such as an egress proxy. It is merged over the default agent, then the guarantees above are
re-applied on top (:func:`agent_fragment`), so an override can't run the agent as root,
add capabilities, or mount the Docker socket. Extra services join the set network.
"""

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, cast

from pydantic import AfterValidator

from sandbox.compose import AGENT_SERVICE, SET_LABEL
from sandbox.compose.web import CA_TARGET, read_only_bind
from sandbox.names import PLAN_FILENAME

AGENT_HOME: Final = "/home/agent"
AGENT_CLAUDE_DIR: Final = f"{AGENT_HOME}/.claude"
WORKSPACE_ROOT: Final = "/workspace"
IMAGE_DIR: Final = Path(__file__).resolve().parent.parent / "images" / "agent"
IMAGE_REPOSITORY: Final = "sandbox-agent"
# Used when sandbox itself runs as root: the agent must never be root.
FALLBACK_USER: Final = "1000:1000"


def workspace(set_name: str) -> str:
    """Where the set directory appears inside the agent."""
    return f"{WORKSPACE_ROOT}/{set_name}"


def default_image() -> str:
    """The default image's tag: content-addressed, so editing the image rebuilds it."""
    digest = hashlib.sha256()
    for path in sorted(p for p in IMAGE_DIR.iterdir() if p.is_file()):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"{IMAGE_REPOSITORY}:{digest.hexdigest()[:12]}"


def agent_user() -> str:
    """The host UID:GID, so files the agent writes stay the developer's."""
    uid, gid = os.getuid(), os.getgid()
    if uid == 0 or gid == 0:
        return FALLBACK_USER
    return f"{uid}:{gid}"


def host_claude_dir() -> Path:
    return Path.home() / ".claude"


# --- the override fragment ---------------------------------------------------------------

OVERRIDE_KEYS: Final = ("services", "volumes", "networks")
SECURITY_OPT: Final = ["no-new-privileges:true"]
# Settings an override may not give the agent at all: each is a way out of the container.
# ``extends`` is among them because Compose merges the extended service in after sandbox
# re-applies its guarantees, so it could bring back any of the others.
FORBIDDEN_AGENT_KEYS: Final = (
    "extends",
    "privileged",
    "cap_add",
    "devices",
    "device_cgroup_rules",
    "gpus",
    "volumes_from",
    "network_mode",
    "pid",
    "ipc",
    "uts",
    "cgroup",
    "userns_mode",
    "use_api_socket",  # mounts the Docker socket
    "group_add",  # e.g. the socket's group
    "runtime",
)
DOCKER_SOCKETS: Final = (PurePosixPath("/var/run/docker.sock"), PurePosixPath("/run/docker.sock"))
SET_NETWORK: Final = "default"


def validate_agent_override(value: dict[str, Any]) -> dict[str, Any]:
    """Pydantic validator for the ``agent`` field of the config, templates and set plans."""
    unknown = sorted(set(value) - set(OVERRIDE_KEYS))
    if unknown:
        raise ValueError(
            f"unknown key {unknown[0]!r}: an agent override is a Compose fragment with "
            f"{', '.join(OVERRIDE_KEYS)} (put agent settings under services.agent)"
        )
    if value.get("services") is not None and not isinstance(value["services"], dict):
        raise ValueError("services must be a mapping of service name to definition")
    services = _mapping(value.get("services"))
    for name, spec in services.items():
        if not isinstance(spec, dict):
            raise ValueError(f"services.{name} must be a mapping")  # noqa: TRY004 - pydantic
    if "build" in _mapping(services.get(AGENT_SERVICE)):
        raise ValueError(
            "services.agent.build is not supported: build the image yourself and set "
            "services.agent.image"
        )
    for key in ("volumes", "networks"):
        if value.get(key) is not None and not isinstance(value[key], dict):
            raise ValueError(f"{key} must be a mapping")
    return value


AgentOverride = Annotated[dict[str, Any], AfterValidator(validate_agent_override)]


def custom_image(override: Mapping[str, Any] | None) -> str | None:
    """The image an override gives the agent, or None to keep the bundled default."""
    image = _agent_spec(override).get("image")
    return str(image) if image is not None else None


def extra_services(override: Mapping[str, Any] | None) -> list[str]:
    """Services an override adds next to the agent (e.g. an egress proxy)."""
    return sorted(name for name in _services(override) if name != AGENT_SERVICE)


def overridden_settings(
    override: Mapping[str, Any] | None,
    set_name: str,
    set_dir: Path,
    sources: Mapping[str, Path],
) -> list[str]:
    """What sandbox will ignore in ``override`` to keep its guarantees (one line each)."""
    guarantees = _guarantees(set_name, set_dir, sources)
    _, notes = _sanitize_agent(_agent_spec(override), guarantees, _declared_volumes(override))
    for name in extra_services(override):
        if "network_mode" in _services(override)[name]:
            notes.append(f"services.{name}.network_mode (extra services join the set network)")
    return notes


def agent_fragment(
    set_name: str,
    set_dir: Path,
    sources: Mapping[str, Path],
    override: Mapping[str, Any] | None = None,
    root_ca: Path | None = None,
) -> dict[str, Any]:
    """The agent service (default, merged with ``override``, guarantees re-applied), plus
    the override's extra services, networks and volumes."""
    # Created here because Docker would otherwise create a missing bind source as root.
    host_claude_dir().mkdir(parents=True, exist_ok=True)
    guarantees = _guarantees(set_name, set_dir, sources, root_ca)
    service: dict[str, Any] = {
        "image": default_image(),
        "build": {"context": str(IMAGE_DIR)},
        # Compose builds the image when it is missing and never pulls it, so an image of
        # the same name on a registry can't stand in for the bundled one.
        "pull_policy": "never",
        "init": True,
        "working_dir": workspace(set_name),
    }
    cleaned, _ = _sanitize_agent(_agent_spec(override), guarantees, _declared_volumes(override))
    if "image" in cleaned:
        # A custom image comes from a registry (or the local cache), not from our build.
        del service["build"]
        del service["pull_policy"]
    extra_volumes = cleaned.pop("volumes", [])
    environment = _environment(cleaned.pop("environment", None))
    labels = _labels(cleaned.pop("labels", None))
    service.update(cleaned)
    service.update(
        {
            "user": guarantees["user"],
            "cap_drop": ["ALL"],
            "security_opt": list(SECURITY_OPT),
            "environment": {**environment, **guarantees["environment"]},
            "volumes": [*extra_volumes, *guarantees["volumes"]],
            "labels": {**labels, SET_LABEL: set_name},
        }
    )
    if "networks" in service:
        service["networks"] = _join_set_network(service["networks"])

    services: dict[str, Any] = {AGENT_SERVICE: service}
    for name in extra_services(override):
        extra = dict(_services(override)[name])
        extra.pop("network_mode", None)
        if "networks" in extra:
            extra["networks"] = _join_set_network(extra["networks"])
        extra["labels"] = {**_labels(extra.get("labels")), SET_LABEL: set_name}
        services[name] = extra
    document: dict[str, Any] = {"services": services}
    for key in ("networks", "volumes"):
        declared = _mapping(override.get(key) if override else None)
        if declared:
            document[key] = {
                name: _labelled(_mapping(spec), set_name) for name, spec in declared.items()
            }
    return document


def _bind(source: Path, target: str) -> dict[str, Any]:
    return {"type": "bind", "source": str(source), "target": target}


def _git_dir_mounts(git_dir: Path) -> list[dict[str, Any]]:
    """A source repo's ``.git``, read-write so the agent can commit, but with its config
    read-only: host-side git reads that config, and a setting the agent chose (a filter or
    ``core.fsmonitor``) would run code on the host. Hooks and ``commondir`` are handled by
    :class:`sandbox.git.SubprocessGit`, which never trusts them."""
    config = git_dir / "config"
    return [_bind(git_dir, str(git_dir)), read_only_bind(config, str(config))]


def _guarantees(
    set_name: str, set_dir: Path, sources: Mapping[str, Path], root_ca: Path | None = None
) -> dict[str, Any]:
    """What sandbox sets on the agent last, whatever an override says.

    The mounts are the only ones an override can't replace, the CA's included.
    """
    plan_target = f"{workspace(set_name)}/{PLAN_FILENAME}"
    environment = {"HOME": AGENT_HOME, "CLAUDE_CONFIG_DIR": AGENT_CLAUDE_DIR}
    volumes = [
        _bind(set_dir, workspace(set_name)),
        # Read-only: the plan pins the agent override, so an agent that could edit it
        # would choose its own settings at the developer's next `up`.
        {**_bind(set_dir / PLAN_FILENAME, plan_target), "read_only": True},
        *(mount for n in sorted(sources) for mount in _git_dir_mounts(sources[n] / ".git")),
        _bind(host_claude_dir(), AGENT_CLAUDE_DIR),
    ]
    if root_ca is not None:
        environment["NODE_EXTRA_CA_CERTS"] = CA_TARGET
        volumes.append(read_only_bind(root_ca, CA_TARGET))
    return {"user": agent_user(), "environment": environment, "volumes": volumes}


def _sanitize_agent(
    spec: Mapping[str, Any],
    guarantees: Mapping[str, Any],
    declared_volumes: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """``spec`` without what would weaken the guarantees, and a note for each removal."""
    cleaned = dict(spec)
    notes: list[str] = []
    for key in FORBIDDEN_AGENT_KEYS:
        if key in cleaned:
            del cleaned[key]
            notes.append(f"services.agent.{key} (not allowed for the agent)")
    reapplied = {
        "user": guarantees["user"],
        "cap_drop": ["ALL"],
        "security_opt": SECURITY_OPT,
    }
    for key, value in reapplied.items():
        if key in cleaned and cleaned.pop(key) != value:
            notes.append(f"services.agent.{key} (sandbox sets {key}: {value})")
    if "environment" in cleaned:
        environment = _environment(cleaned["environment"])
        for key, value in guarantees["environment"].items():
            if key in environment and environment.pop(key) != value:
                notes.append(f"services.agent.environment.{key} (sandbox sets {key}={value})")
        cleaned["environment"] = environment
    if "volumes" in cleaned:
        reserved = {volume["target"] for volume in guarantees["volumes"]}
        kept: list[Any] = []
        for volume in cast(list[Any], cleaned["volumes"] or []):
            source, target = _volume_ends(volume)
            refusal = _socket_refusal(volume, declared_volumes)
            if refusal is not None:
                notes.append(f"volume {source} ({refusal})")
            elif target in reserved:
                notes.append(f"volume on {target} (sandbox mounts {target} itself)")
            else:
                kept.append(volume)
        cleaned["volumes"] = kept
    return cleaned, notes


def _volume_ends(volume: object) -> tuple[str, str]:
    """(source, target) of a short-syntax string or long-syntax mapping volume."""
    if isinstance(volume, str):
        parts = volume.split(":")
        return (parts[0], parts[1]) if len(parts) > 1 else ("", parts[0])
    spec = _mapping(volume)
    return str(spec.get("source", "")), str(spec.get("target", ""))


SOCKET_REFUSAL: Final = "would expose the Docker socket"
RELATIVE_REFUSAL: Final = (
    "relative bind sources are not allowed for the agent: Compose resolves them against "
    "the project directory, so use an absolute path"
)


def _socket_refusal(volume: object, declared_volumes: Mapping[str, Any]) -> str | None:
    """Why ``volume`` may not be mounted on the agent, or None if it is safe.

    Bind sources are resolved as Compose resolves them (``~`` against the home directory)
    and refused when they are, or contain, a Docker socket; a relative source is refused
    outright because it depends on the project directory. A named volume is checked
    through its declaration: a ``local`` volume with a bind ``device`` is a bind mount too.
    """
    source, _ = _volume_ends(volume)
    if not source:
        return None  # an anonymous volume
    kind = _mapping(volume).get("type")
    if kind == "bind" or source.startswith(("/", ".", "~")):
        return _host_path_refusal(source)
    if kind not in (None, "volume"):
        return None  # tmpfs, npipe, ...: no host path
    declaration = _mapping(declared_volumes.get(source))
    device = _mapping(declaration.get("driver_opts")).get("device")
    if device is None:
        return None
    refusal = _host_path_refusal(str(device))
    return f"its declared device {refusal}" if refusal is not None else None


def _host_path_refusal(source: str) -> str | None:
    try:
        expanded = str(Path(source).expanduser())
    except RuntimeError:  # ``~user`` for an unknown user: Compose can't resolve it either
        return RELATIVE_REFUSAL
    if not expanded.startswith("/"):
        return RELATIVE_REFUSAL
    return SOCKET_REFUSAL if _exposes_docker_socket(expanded) else None


def _exposes_docker_socket(source: str) -> bool:
    """True for a socket path itself, or any directory that contains one (``/var/run``).

    Checked both as written and with symlinks resolved, since Docker follows them.
    """
    sockets = docker_sockets()
    for candidate in {os.path.normpath(source), os.path.realpath(source)}:
        path = PurePosixPath(candidate)
        if path.name == "docker.sock":
            return True
        if any(path == socket or path in socket.parents for socket in sockets):
            return True
    return False


def docker_sockets() -> set[PurePosixPath]:
    """Every Docker socket this host may be using: the default paths, rootless Docker's
    (``$XDG_RUNTIME_DIR/docker.sock``), ``DOCKER_HOST``'s and the current docker context's
    unix socket, each also with symlinks resolved."""
    hosts = [os.environ.get("DOCKER_HOST", ""), _context_host()]
    paths = [*map(str, DOCKER_SOCKETS), f"/run/user/{os.getuid()}/docker.sock"]
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        paths.append(f"{runtime_dir}/docker.sock")
    paths += [host.removeprefix("unix://") for host in hosts if host.startswith("unix://")]
    found: set[PurePosixPath] = set()
    for path in paths:
        if path.startswith("/"):
            found |= {PurePosixPath(os.path.normpath(path)), PurePosixPath(os.path.realpath(path))}
    return found


def _context_host() -> str:
    """The docker endpoint of the current docker context (``DOCKER_CONTEXT`` or the CLI
    config's ``currentContext``), or "" for the default context or an unreadable one."""
    config_dir = Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker")
    name = os.environ.get("DOCKER_CONTEXT")
    try:
        if not name:
            name = _json_mapping(config_dir / "config.json").get("currentContext")
        if not isinstance(name, str) or name in ("", "default"):
            return ""
        meta_dir = config_dir / "contexts" / "meta" / hashlib.sha256(name.encode()).hexdigest()
        endpoints = _mapping(_json_mapping(meta_dir / "meta.json").get("Endpoints"))
        host = _mapping(endpoints.get("docker")).get("Host")
    except (OSError, ValueError):
        return ""
    return host if isinstance(host, str) else ""


def _json_mapping(path: Path) -> Mapping[str, Any]:
    return _mapping(json.loads(path.read_text()))


def _join_set_network(networks: object) -> object:
    if isinstance(networks, list):
        names = cast(list[Any], networks)
        return names if SET_NETWORK in names else [*names, SET_NETWORK]
    attached = dict(_mapping(networks))
    attached.setdefault(SET_NETWORK, None)
    return attached


def _labelled(spec: Mapping[str, Any], set_name: str) -> dict[str, Any]:
    if spec.get("external"):
        return dict(spec)  # not ours to label
    return {**spec, "labels": {**_labels(spec.get("labels")), SET_LABEL: set_name}}


def _environment(value: object) -> dict[str, Any]:
    """Compose's list (``KEY=value``) or mapping form of ``environment``, as a mapping."""
    if isinstance(value, list):
        items = (str(item).partition("=") for item in cast(list[Any], value))
        # A bare `KEY` passes the variable through from the host: null in mapping form.
        return {key: val if sep else None for key, sep, val in items}
    return dict(_mapping(value))


def _labels(value: object) -> dict[str, Any]:
    return _environment(value)  # same list-or-mapping shape


def _services(override: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    services = _mapping(override.get("services") if override else None)
    return {str(name): dict(_mapping(spec)) for name, spec in services.items()}


def _declared_volumes(override: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return _mapping(override.get("volumes") if override else None)


def _agent_spec(override: Mapping[str, Any] | None) -> dict[str, Any]:
    return _services(override).get(AGENT_SERVICE, {})


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}
