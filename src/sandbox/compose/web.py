"""Override fragment for HTTPS web apps (see ``sandbox.web``) and the set's CA mounts.

A web app is a repo's service marked ``x-sandbox: {web: true}`` and named after the repo.
Its service gets:

- port 443 published on its own leased IP (its hostname ``<repo>.<set>.test`` is the
  service's set hostname, so ``sandbox.compose.network`` already aliases it);
- ``net.ipv4.ip_unprivileged_port_start=0``, so the app listens on 443 as non-root;
- the set's leaf certificate read-only at ``/sandbox/tls/`` (``cert.pem``, ``key.pem``);
- ``WORKTREE`` (its repo's worktree) and ``APP_URL`` (``https://<hostname>``) in its
  environment, and nothing else.

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

    @property
    def hostname(self) -> str:
        return hostname(self.service, self.set_name)

    @property
    def url(self) -> str:
        return f"https://{self.hostname}"


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
        service["environment"] = {"WORKTREE": str(app.worktree), "APP_URL": app.url}
        if tls_dir is not None:
            service["volumes"].append(read_only_bind(tls_dir, TLS_TARGET))
        ip = ips.get(app.service)
        declared = container_ports(app.service, definitions.get(app.service, {}))
        if ip is not None and HTTPS_PORT not in declared:
            # Appended to the IP-bound ports sandbox.compose.network restates (if any).
            service["ports"] = [f"{ip}:{HTTPS_PORT.port}:{HTTPS_PORT.port}/{HTTPS_PORT.protocol}"]
    return {"services": services}
