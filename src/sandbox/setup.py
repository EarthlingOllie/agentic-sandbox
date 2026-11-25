"""``sudo sandbox setup``: install, once, everything sandbox needs root for.

After it, the everyday ``sandbox`` runs unprivileged:

- **Resolver configuration.** The operating system is told to send lookups under the domain
  to the resolver (``sandbox.resolver_config``): ``/etc/resolver/<domain>`` on macOS, a
  systemd-resolved drop-in and a reload on Linux. A Linux machine without systemd-resolved
  is refused before anything is installed, with the line to add by hand. ``--domain`` chooses
  the domain and is written into the invoking user's config, as that user, leaving the
  file's other keys alone; without it the configured domain (``test`` by default) is used
  and the config is not touched. A rerun with another domain removes the previous domain's
  configuration. The configuration is itself the record of the installed domain
  (:func:`sandbox.resolver_config.installed_domain`).
- **mkcert CA.** ``mkcert -install``, run as ``SUDO_USER`` (never as root), so the CA lands
  in that user's CAROOT and their browser trusts it.

Setup starts no containers: the resolver itself is started by ``up``. ``--uninstall``
removes the resolver configuration of whichever domain was installed, then stops and removes
the resolver, through Docker as the invoking user. It leaves the mkcert CA, printing the
``mkcert -uninstall`` command instead.

Both directions also remove what an earlier version of sandbox installed on the machine and
nothing uses any more (``sandbox.leftovers``), so that nothing privileged lingers after an
upgrade. Every system path is taken relative to :attr:`Host.root`, and every command goes
through :attr:`Host.run`, so tests run setup against a temp directory with a fake runner.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import yaml

from sandbox import resolver_config
from sandbox.compose.resolver import RESOLVER_ADDRESS, RESOLVER_CONTAINER, RESOLVER_PORT
from sandbox.config import Config, load_config
from sandbox.docker import Docker
from sandbox.errors import SandboxError
from sandbox.leftovers import remove_leftovers
from sandbox.names import validate_domain
from sandbox.platforms import Host
from sandbox.resolver_config import RESOLVED_DROP_IN, RESOLVED_UNIT
from sandbox.yamlio import dump_yaml

SETUP_COMMAND: Final = "sudo sandbox setup"
_USER_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_DOMAIN_KEY: Final = re.compile(r"domain\s*:")
# $1: the file, $2: its new content. Written beside the file and moved over it.
_WRITE_FILE: Final = (
    'mkdir -p -- "$(dirname -- "$1")" && printf %s "$2" > "$1.sandbox-tmp" '
    '&& mv -f -- "$1.sandbox-tmp" "$1"'
)


@dataclass(frozen=True)
class SetupResult:
    user: str
    lines: list[str]


async def install(host: Host, domain: str | None = None) -> SetupResult:
    """Point the machine at the resolver for the domain and install the mkcert CA.

    ``domain`` (``--domain``) is recorded in the invoking user's config; without it the
    configured domain is used, which is ``test`` unless the config says otherwise.
    """
    user = _require_root(host)
    config_path = _user_config_path(host, user)
    config = _load_user_config(host, config_path)
    chosen = _chosen_domain(domain, config)
    mkcert = _find_mkcert(host)
    await _require_resolver_support(host, chosen)

    lines: list[str] = []
    if domain is not None:
        await _record_domain(host, user, config_path, chosen)
        lines.append(f"config: `domain: {chosen}` written to {config_path} as {user}")
    replaced = await resolver_config.install(host, chosen)
    target = resolver_config.macos_resolver_file(chosen) if host.is_macos else RESOLVED_DROP_IN
    lines.append(
        f"resolver: names under .{chosen} are looked up at {RESOLVER_ADDRESS} port "
        f"{RESOLVER_PORT} ({target}"
        + ("" if host.is_macos else f", {RESOLVED_UNIT} reloaded")
        + ")"
    )
    lines += [f"removed {path} (the previous domain)" for path in replaced]
    lines += await remove_leftovers(host)
    await _mkcert_install(host, user, mkcert)
    lines.append(f"mkcert: CA installed for {user} (`mkcert -install` as {user})")
    return SetupResult(user, lines)


async def uninstall(host: Host, docker: Docker) -> SetupResult:
    """Remove the resolver configuration (of whichever domain was installed) and the
    resolver; keep the mkcert CA."""
    user = host.env.get("SUDO_USER", "")
    _require_euid_root(host)
    lines = [f"removed {path}" for path in await resolver_config.remove(host)]
    lines += await remove_leftovers(host)
    # Last, and with the invoking user's Docker: root's may not be the one they use (on
    # macOS the Docker context lives in the user's account).
    try:
        await docker.remove_resolver(as_user=user if _valid_user(user) else None)
    except SandboxError as exc:
        raise SandboxError(
            "everything else setup installed was removed, but the resolver could not be "
            f"removed:\n{exc}\nWith Docker running, rerun `{SETUP_COMMAND} --uninstall`, or "
            f"remove it yourself: docker rm --force {RESOLVER_CONTAINER}"
        ) from exc
    lines.append(f"removed the resolver (container {RESOLVER_CONTAINER}), if there was one")
    lines.append(
        "the mkcert CA was left installed (other tools may rely on it); "
        "to remove it, run as yourself: mkcert -uninstall"
    )
    return SetupResult(user, lines)


# --- checks ----------------------------------------------------------------------------------


def _require_euid_root(host: Host) -> None:
    if host.euid != 0:
        raise SandboxError(f"setup needs root: run `{SETUP_COMMAND}`")


def _valid_user(user: str) -> bool:
    return bool(user) and user != "root" and bool(_USER_NAME.fullmatch(user))


def _require_root(host: Host) -> str:
    """The invoking (non-root) user; setup itself must run as root."""
    _require_euid_root(host)
    user = host.env.get("SUDO_USER", "")
    if not _valid_user(user):
        raise SandboxError(
            "setup installs things for the user who runs it: run it with sudo from your own "
            f"account (`{SETUP_COMMAND}`), not from a root shell"
        )
    return user


def _user_config_path(host: Host, user: str) -> Path:
    """The invoking user's config file (sudo resets HOME, so it is found through their
    account); it need not exist unless ``SANDBOX_CONFIG`` names it."""
    named = host.env.get("SANDBOX_CONFIG")
    if named:
        return Path(named)
    home = host.homes(user)
    if home is None:
        raise SandboxError(f"user {user!r} has no home directory")
    return home / ".config" / "sandbox" / "config.yaml"


def _load_user_config(host: Host, path: Path) -> Config:
    if host.env.get("SANDBOX_CONFIG") or path.is_file():
        return load_config(path)
    return Config()


def _chosen_domain(requested: str | None, config: Config) -> str:
    if requested is None:
        return config.domain
    try:
        return validate_domain(requested)
    except ValueError as exc:
        raise SandboxError(f"--domain: {exc}") from exc


async def _require_resolver_support(host: Host, domain: str) -> None:
    """Refuse a Linux machine whose lookups setup cannot point at the resolver."""
    if host.is_macos or await resolver_config.has_systemd_resolved(host):
        return
    raise SandboxError(
        "systemd-resolved is not running on this machine, and setup can only point "
        "systemd-resolved at the resolver; nothing was installed.\n"
        f"Send .{domain} to the resolver yourself: with dnsmasq as this machine's DNS server "
        "(NetworkManager's dnsmasq included), add this line to its configuration and restart "
        f"it:\n  {resolver_config.manual_line(domain)}\n"
        f"Any other DNS server needs the same rule: forward {domain} to {RESOLVER_ADDRESS} "
        f"port {RESOLVER_PORT}."
    )


def _find_mkcert(host: Host) -> str:
    found = shutil.which("mkcert", path=host.env.get("PATH", ""))
    if found is None:
        raise SandboxError(
            "mkcert is not installed or not on PATH; install it (https://github.com/FiloSottile/"
            "mkcert), then rerun setup. If it is installed, sudo may have reset PATH: run "
            '`sudo env "PATH=$PATH" sandbox setup`'
        )
    return found


def config_with_domain(text: str, domain: str) -> str:
    """``text`` (a config file) with its ``domain`` key set, everything else as it was."""
    entry = yaml.safe_dump({"domain": domain}, default_flow_style=False).strip()
    lines = text.splitlines()
    before = _mapping(text)
    found = [index for index, line in enumerate(lines) if _DOMAIN_KEY.match(line)]
    if len(found) == 1:
        edited = "\n".join([*lines[: found[0]], entry, *lines[found[0] + 1 :]]) + "\n"
    elif not found and "domain" not in (before or {}):
        edited = "\n".join([*lines, entry]) + "\n"
    else:
        edited = ""
    if edited and _mapping(edited) == {**(before or {}), "domain": domain}:
        return edited
    # Not a layout a line edit is safe on (a flow mapping, a quoted key): rewrite it.
    return dump_yaml({**(before or {}), "domain": domain})


def _mapping(text: str) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if data is None:
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


async def _record_domain(host: Host, user: str, path: Path, domain: str) -> None:
    """Write ``domain`` into the user's config, as that user: root never creates or owns
    anything in their home directory."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    argv = [*_as_user(user), "/bin/sh", "-c", _WRITE_FILE, "sh", str(path),
            config_with_domain(text, domain)]  # fmt: skip
    status, output = await host.run(argv)
    if status != 0:
        raise SandboxError(f"could not write `domain: {domain}` to {path} as {user}: {output}")


def _as_user(user: str) -> list[str]:
    return ["sudo", "-u", user, "-H", "--"]


async def _mkcert_install(host: Host, user: str, mkcert: str) -> None:
    argv = [*_as_user(user), mkcert, "-install"]
    status, output = await host.run(argv)
    if status != 0:
        raise SandboxError(f"`mkcert -install` as {user} failed:\n{output}")
