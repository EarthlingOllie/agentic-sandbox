"""A set's bindings: every hostname, its leased loopback IP and its native ports.

:func:`lease_ips` is the ``up`` validation step that leases them (``sandbox.leases``). It is
the one validation step that writes host state, because a lease is sticky and idempotent:
it is never rolled back, and only ``prune`` releases it. Running it before anything starts
means an exhausted pool fails ``up`` cleanly, and the no-op path can still print bindings.
"""

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


def lease_store(config: Config) -> LeaseStore:
    return LeaseStore(
        config.state_dir / LEASE_FILENAME, config.lock_dir / "leases.lock", config.ip_pool
    )


async def lease_ips(ctx: UpContext) -> None:
    """Lease the set's /24 and one IP per service hostname (kept if already leased)."""
    if not ctx.graph.services:
        return
    hostnames = [hostname(name, ctx.set_name) for name in ctx.graph.names()]
    ctx.lease = await lease_store(ctx.config).lease(ctx.set_name, hostnames)


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


def bindings(set_name: str, graph: ServiceGraph, lease: SetLease | None) -> list[Binding]:
    ips = service_ips(set_name, graph, lease)
    return [
        Binding(
            hostname(name, set_name),
            ip,
            tuple(container_ports(name, graph.services[name].definition)),
        )
        for name, ip in ips.items()
    ]


def format_bindings(found: list[Binding]) -> list[str]:
    """One line per hostname: ``<hostname>  <ip>  <port>/<proto>, ...``."""
    lines: list[str] = []
    for binding in found:
        ports = ", ".join(str(port) for port in binding.ports) or "(no ports)"
        lines.append(f"{binding.hostname}  {binding.ip}  {ports}")
    return lines
