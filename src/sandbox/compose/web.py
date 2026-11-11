"""Override fragment for HTTPS web apps (see ``sandbox.web``) and the set's CA mounts.

A web app is a repo's service marked ``x-sandbox: {web: true}`` and named after the repo.
Its service gets:

- port 443 published on its own leased IP (its hostname ``<repo>.<set>.test`` is the
  service's set hostname, so ``sandbox.compose.network`` already aliases it);
- ``net.ipv4.ip_unprivileged_port_start=0``, so the app listens on 443 as non-root;
- the set's leaf certificate read-only at ``/sandbox/tls/`` (``cert.pem``, ``key.pem``);
- ``WORKTREE`` (its repo's worktree) and ``APP_URL`` (``https://<hostname>``) in its
  environment, and nothing else;
- with ``hosts: [admin, portal]``, the extra hostnames ``admin.<set>.test`` and
  ``portal.<set>.test`` on the same container and IP: ``sandbox.compose.network`` aliases
  them (from :func:`extra_hostnames`), the hosts block lists them, and the set's
  ``*.<set>.test`` certificate covers them (each is one label deep);
- with ``vite: 5173``, that port published on its IP too, and ``VITE_HMR_HOST`` (its
  hostname) in its environment for the app's ``server.hmr.host``.

When the set has a web app, every service also gets the machine CA's ``rootCA.pem``
read-only at ``/sandbox/ca/rootCA.pem`` (the agent gets it from ``sandbox.compose.agent``),
never the CA key.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Final

from sandbox.compose.network import ContainerPort, container_ports, hostname

CA_TARGET: Final = "/sandbox/ca/rootCA.pem"
TLS_TARGET: Final = "/sandbox/tls"
HTTPS_PORT: Final = ContainerPort(443)
UNPRIVILEGED_PORTS: Final = {"net.ipv4.ip_unprivileged_port_start": 0}


@dataclass(frozen=True)
class WebApp:
    repo: str
    # The Compose service: always named after the repo.
    service: str
    set_name: str
    # The repo's worktree in the set directory.
    worktree: Path
    # Extra one-label hosts (``admin`` -> ``admin.<set>.test``) on the same container and IP.
    hosts: tuple[str, ...] = ()
    # The Vite dev server's port, if any: published on the app's IP, HMR host set.
    vite: int | None = None

    @property
    def hostname(self) -> str:
        return hostname(self.service, self.set_name)

    @property
    def extra_hostnames(self) -> tuple[str, ...]:
        return tuple(hostname(host, self.set_name) for host in self.hosts)

    @property
    def hostnames(self) -> tuple[str, ...]:
        """Every hostname the app answers to: its own first, then its extra hosts."""
        return (self.hostname, *self.extra_hostnames)

    @property
    def url(self) -> str:
        return f"https://{self.hostname}"


def extra_hostnames(web_apps: Sequence[WebApp]) -> dict[str, tuple[str, ...]]:
    """Web service name -> its extra hostnames (only apps that declare some)."""
    return {app.service: app.extra_hostnames for app in web_apps if app.hosts}


def read_only_bind(source: Path, target: str) -> dict[str, Any]:
    return {"type": "bind", "source": str(source), "target": target, "read_only": True}


def web_fragment(
    web_apps: Sequence[WebApp],
    definitions: Mapping[str, Mapping[str, Any]],
    ips: Mapping[str, IPv4Address],
    root_ca: Path | None,
    tls_dir: Path | None,
    other_services: Iterable[str] = (),
) -> dict[str, Any]:
    """The web apps' settings, plus the CA mount on every service in ``definitions`` and
    ``other_services`` (none without a web app)."""
    if not web_apps:
        return {}
    services: dict[str, Any] = {}
    if root_ca is not None:
        for name in sorted({*definitions, *other_services}):
            services[name] = {"volumes": [read_only_bind(root_ca, CA_TARGET)]}
    for app in web_apps:
        service: dict[str, Any] = services.setdefault(app.service, {"volumes": []})
        service["sysctls"] = dict(UNPRIVILEGED_PORTS)
        environment = {"WORKTREE": str(app.worktree), "APP_URL": app.url}
        if app.vite is not None:
            environment["VITE_HMR_HOST"] = app.hostname
        service["environment"] = environment
        if tls_dir is not None:
            service["volumes"].append(read_only_bind(tls_dir, TLS_TARGET))
        ip = ips.get(app.service)
        declared = container_ports(app.service, definitions.get(app.service, {}))
        wanted = [HTTPS_PORT, *([ContainerPort(app.vite)] if app.vite is not None else [])]
        ports = [f"{ip}:{p.port}:{p.port}/{p.protocol}" for p in wanted if p not in declared]
        if ip is not None and ports:
            # Appended to the IP-bound ports sandbox.compose.network restates (if any).
            service["ports"] = ports
    return {"services": services}
