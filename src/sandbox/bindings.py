"""A set's bindings: every hostname, its leased loopback IP and its native ports.

:func:`lease_ips` is the ``up`` validation step that leases them (``sandbox.leases``). It is
the one validation step that writes host state, because a lease is sticky and idempotent:
it is never rolled back, and only ``prune`` releases it. Running it before anything starts
means an exhausted pool fails ``up`` cleanly, and the no-op path can still print bindings.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address

from sandbox.compose.dedup import ServiceGraph
from sandbox.compose.network import ContainerPort, container_ports, hostname
from sandbox.config import Config
from sandbox.leases import LeaseStore, SetLease
from sandbox.pipeline import UpContext

LEASE_FILENAME = "leases.json"


@dataclass(frozen=True)
class Binding:
    hostname: str
    ip: IPv4Address
    ports: tuple[ContainerPort, ...]


def lease_store(config: Config, max_blocks: int | None = None) -> LeaseStore:
    return LeaseStore(
        config.state_dir / LEASE_FILENAME,
        config.lock_dir / "leases.lock",
        config.ip_pool,
        max_blocks,
    )


async def lease_ips(ctx: UpContext) -> None:
    """Lease the set's /24 and one IP per service hostname (kept if already leased)."""
    if not ctx.graph.services:
        return
    hostnames = [hostname(name, ctx.set_name) for name in ctx.graph.names()]
    ctx.lease = await lease_store(ctx.config, ctx.alias_blocks).lease(ctx.set_name, hostnames)


def service_ips(
    set_name: str, graph: ServiceGraph, lease: SetLease | None
) -> dict[str, IPv4Address]:
    """Service name -> its leased IP."""
    if lease is None:
        return {}
    return {
        name: lease.ips[hostname(name, set_name)]
        for name in graph.names()
        if hostname(name, set_name) in lease.ips
    }


def bindings(
    set_name: str,
    graph: ServiceGraph,
    lease: SetLease | None,
    extra_hostnames: Mapping[str, Sequence[str]] | None = None,
) -> list[Binding]:
    """Each service's hostname, then its extra hostnames (service name -> names, e.g. a web
    app's ``hosts``), on the service's IP with its ports."""
    found: list[Binding] = []
    for name, ip in service_ips(set_name, graph, lease).items():
        ports = tuple(container_ports(name, graph.services[name].definition))
        for host in (hostname(name, set_name), *(extra_hostnames or {}).get(name, ())):
            found.append(Binding(host, ip, ports))
    return found


def format_bindings(found: list[Binding]) -> list[str]:
    """One line per hostname: ``<hostname>  <ip>  <port>/<proto>, ...``."""
    lines: list[str] = []
    for binding in found:
        ports = ", ".join(str(port) for port in binding.ports) or "(no ports)"
        lines.append(f"{binding.hostname}  {binding.ip}  {ports}")
    return lines
