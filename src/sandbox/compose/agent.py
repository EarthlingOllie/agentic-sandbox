"""The agent container: the override fragment that adds a hardened ``agent`` service.

Every set gets one. It runs the bundled default image (``sandbox/images/agent``, built by
Compose on first use and tagged with a hash of its build context, so an updated image is
rebuilt) as the developer's
host UID:GID, never root, with every capability dropped, ``no-new-privileges``, and no
Docker socket. It sees exactly:

- the set directory at ``/workspace/<set>/`` (where shells start);
- each source repo's ``.git`` read-write at its exact host path, because a worktree's
  ``.git`` file points there; the source's working tree itself is never mounted;
- the host's ``~/.claude/`` at ``/home/agent/.claude`` (``CLAUDE_CONFIG_DIR``), so every
  set shares one login. The host's ``~/.claude.json`` is not mounted.

An **agent override** (a Compose fragment from the set plan, pinned there from a template
or the global config) may replace the image, add settings to the agent and add services
such as an egress proxy. It is merged over the default agent, then the guarantees above are
re-applied on top (:func:`agent_fragment`), so an override can't run the agent as root,
add capabilities, or mount the Docker socket. Extra services join the set network.
"""

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, cast

from pydantic import AfterValidator

from sandbox.compose import AGENT_SERVICE, SET_LABEL

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
FORBIDDEN_AGENT_KEYS: Final = (
    "privileged",
    "cap_add",
    "devices",
    "volumes_from",
    "network_mode",
    "pid",
    "ipc",
    "userns_mode",
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
    _, notes = _sanitize_agent(_agent_spec(override), guarantees)
    for name in extra_services(override):
        if "network_mode" in _services(override)[name]:
            notes.append(f"services.{name}.network_mode (extra services join the set network)")
    return notes


def agent_fragment(
    set_name: str,
    set_dir: Path,
    sources: Mapping[str, Path],
    override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The agent service (default, merged with ``override``, guarantees re-applied), plus
    the override's extra services, networks and volumes."""
    # Created here because Docker would otherwise create a missing bind source as root.
    host_claude_dir().mkdir(parents=True, exist_ok=True)
    guarantees = _guarantees(set_name, set_dir, sources)
    service: dict[str, Any] = {
        "image": default_image(),
        "build": {"context": str(IMAGE_DIR)},
        # Compose builds the image when it is missing and never pulls it, so an image of
        # the same name on a registry can't stand in for the bundled one.
        "pull_policy": "never",
        "init": True,
        "working_dir": workspace(set_name),
    }
    cleaned, _ = _sanitize_agent(_agent_spec(override), guarantees)
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


def _guarantees(set_name: str, set_dir: Path, sources: Mapping[str, Path]) -> dict[str, Any]:
    """What sandbox sets on the agent last, whatever an override says.

    The mounts are the only ones an override can't replace; a CA mount joins them once
    sets have a CA (HTTPS web apps).
    """
    return {
        "user": agent_user(),
        "environment": {"HOME": AGENT_HOME, "CLAUDE_CONFIG_DIR": AGENT_CLAUDE_DIR},
        "volumes": [
            _bind(set_dir, workspace(set_name)),
            *(_bind(sources[n] / ".git", str(sources[n] / ".git")) for n in sorted(sources)),
            _bind(host_claude_dir(), AGENT_CLAUDE_DIR),
        ],
    }


def _sanitize_agent(
    spec: Mapping[str, Any], guarantees: Mapping[str, Any]
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
            if _exposes_docker_socket(source):
                notes.append(f"volume {source} (would expose the Docker socket)")
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


def _exposes_docker_socket(source: str) -> bool:
    """True for a socket path itself, or any directory that contains one (``/var/run``)."""
    if not source.startswith("/"):
        return False  # a named volume
    path = PurePosixPath(os.path.normpath(source))
    if path.name == "docker.sock":
        return True
    return any(path == socket or path in socket.parents for socket in DOCKER_SOCKETS)


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


def _agent_spec(override: Mapping[str, Any] | None) -> dict[str, Any]:
    return _services(override).get(AGENT_SERVICE, {})


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}
