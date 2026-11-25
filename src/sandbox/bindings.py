"""A set's bindings: every set hostname and the conventional ports it answers on.

Nothing is allocated: a hostname is reached at its container's own address, which Docker
assigns and the resolver answers (``sandbox.resolver``), on the container-side ports its
service declares. A web app also answers on 443, and on its Vite port if it has one,
declared or not. ``up`` prints these; ``urls`` lists the same addresses.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sandbox.compose.dedup import ServiceGraph
from sandbox.compose.network import ContainerPort, container_ports, hostname
from sandbox.compose.web import WebApp
from sandbox.config import Config


@dataclass(frozen=True)
class Binding:
    hostname: str
    ports: tuple[ContainerPort, ...]


def up_lock_path(config: Config, set_name: str) -> Path:
    """Held by ``up`` of ``set_name`` while it runs, so bare ``prune`` leaves the set alone."""
    return config.lock_dir / f"up-{set_name}.lock"


def service_ports(
    name: str, graph: ServiceGraph, web_apps: Sequence[WebApp] = ()
) -> tuple[ContainerPort, ...]:
    """The ports ``name`` answers on: the ones it declares, then a web app's own."""
    declared = container_ports(name, graph.services[name].definition)
    contract = [p for app in web_apps if app.service == name for p in app.ports]
    return tuple(dict.fromkeys([*declared, *contract]))


def bindings(
    set_name: str, domain: str, graph: ServiceGraph, web_apps: Sequence[WebApp] = ()
) -> list[Binding]:
    """Each service's hostname, then a web app's extra hostnames, with the service's ports."""
    extra = {app.service: app.extra_hostnames for app in web_apps}
    found: list[Binding] = []
    for name in graph.names():
        ports = service_ports(name, graph, web_apps)
        for host in (hostname(name, set_name, domain), *extra.get(name, ())):
            found.append(Binding(host, ports))
    return found


def format_bindings(found: list[Binding]) -> list[str]:
    """One line per hostname: ``<hostname>  <port>/<proto>, ...``."""
    lines: list[str] = []
    for binding in found:
        ports = ", ".join(str(port) for port in binding.ports) or "(no ports)"
        lines.append(f"{binding.hostname}  {ports}")
    return lines
