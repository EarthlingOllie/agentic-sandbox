"""The operating system's per-domain resolver configuration.

``sudo sandbox setup`` (``sandbox.setup``) writes it, so that the machine sends lookups of
names under the domain, and only those, to the resolver's loopback address and port
(``sandbox.compose.resolver``):

- **macOS**: ``/etc/resolver/<domain>``, the per-domain resolver file (``resolver(5)``).
- **Linux with systemd-resolved**: one drop-in, ``/etc/systemd/resolved.conf.d/sandbox.conf``,
  naming the resolver as a DNS server and the domain as a routing-only domain (``~domain``),
  followed by a reload of systemd-resolved. The ``address:port`` form of ``DNS=`` needs
  systemd 246 or later.
- **Linux without systemd-resolved**: nothing is installed; :func:`manual_line` is the line
  a developer adds to their own DNS server.

The configuration is also the record of which domain setup installed:
:func:`installed_domain` reads it back, without root, for ``up`` to compare with the config.
On macOS a file in ``/etc/resolver`` is sandbox's when it names the resolver's address and
port; anything else there belongs to another tool and is never touched.
"""

from pathlib import Path
from typing import Final

from sandbox.compose.resolver import RESOLVER_ADDRESS, RESOLVER_PORT
from sandbox.errors import SandboxError
from sandbox.platforms import Host

MACOS_RESOLVER_DIR: Final = Path("/etc/resolver")
RESOLVED_DROP_IN: Final = Path("/etc/systemd/resolved.conf.d/sandbox.conf")
RESOLVED_UNIT: Final = "systemd-resolved.service"
_MACOS_SETTINGS: Final = (f"nameserver {RESOLVER_ADDRESS}", f"port {RESOLVER_PORT}")
_ROUTING_DOMAIN: Final = "Domains=~"


def macos_resolver_file(domain: str) -> Path:
    return MACOS_RESOLVER_DIR / domain


def macos_resolver_text() -> str:
    return "".join(f"{line}\n" for line in _MACOS_SETTINGS)


def resolved_drop_in_text(domain: str) -> str:
    return (
        "# Installed by `sudo sandbox setup`: sends lookups under this domain, and only\n"
        "# those, to sandbox's resolver. Remove with `sudo sandbox setup --uninstall`.\n"
        "[Resolve]\n"
        f"DNS={RESOLVER_ADDRESS}:{RESOLVER_PORT}\n"
        f"{_ROUTING_DOMAIN}{domain}\n"
    )


def manual_line(domain: str) -> str:
    """What sends the domain to the resolver in a dnsmasq-style DNS server's configuration."""
    return f"server=/{domain}/{RESOLVER_ADDRESS}#{RESOLVER_PORT}"


def _settings(text: str) -> list[str]:
    stripped = (line.strip() for line in text.splitlines())
    return [line for line in stripped if line and line[0] not in "#;"]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def installed_domains(host: Host) -> list[str]:
    """Every domain this machine sends to the resolver (one, after a setup that finished)."""
    if not host.is_macos:
        text = _read(host.path(RESOLVED_DROP_IN))
        lines = _settings(text) if text is not None else []
        return [
            line.removeprefix(_ROUTING_DOMAIN) for line in lines if line.startswith(_ROUTING_DOMAIN)
        ]
    directory = host.path(MACOS_RESOLVER_DIR)
    if not directory.is_dir():
        return []
    found: list[str] = []
    for path in sorted(directory.iterdir()):
        text = _read(path) if path.is_file() else None
        if text is not None and tuple(_settings(text)) == _MACOS_SETTINGS:
            found.append(path.name)
    return found


def installed_domain(host: Host) -> str | None:
    """The domain setup pointed this machine at the resolver for; None when it has not."""
    domains = installed_domains(host)
    return domains[0] if domains else None


async def has_systemd_resolved(host: Host) -> bool:
    status, _ = await host.run(["systemctl", "is-active", "--quiet", RESOLVED_UNIT])
    return status == 0


async def _reload_resolved(host: Host) -> None:
    argv = ["systemctl", "reload-or-restart", RESOLVED_UNIT]
    status, output = await host.run(argv)
    if status != 0:
        raise SandboxError(f"`{' '.join(argv)}` failed: {output}")


def _write(path: Path, text: str) -> None:
    """Replace ``path`` atomically, world-readable (the stub resolver does not run as root)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.sandbox-tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(path)


async def install(host: Host, domain: str) -> list[Path]:
    """Point the machine at the resolver for ``domain`` and for no other domain; the system
    paths of an earlier domain's configuration that was removed."""
    if host.is_macos:
        removed = [
            macos_resolver_file(previous)
            for previous in installed_domains(host)
            if previous != domain
        ]
        _write(host.path(macos_resolver_file(domain)), macos_resolver_text())
        for path in removed:
            host.path(path).unlink(missing_ok=True)
        return removed
    _write(host.path(RESOLVED_DROP_IN), resolved_drop_in_text(domain))
    await _reload_resolved(host)
    return []


async def remove(host: Host) -> list[Path]:
    """Remove the configuration of whichever domain was installed; the system paths removed."""
    if host.is_macos:
        removed = [macos_resolver_file(domain) for domain in installed_domains(host)]
        for path in removed:
            host.path(path).unlink(missing_ok=True)
        return removed
    drop_in = host.path(RESOLVED_DROP_IN)
    if not drop_in.exists():
        return []
    drop_in.unlink()
    if await has_systemd_resolved(host):
        await _reload_resolved(host)
    return [RESOLVED_DROP_IN]
