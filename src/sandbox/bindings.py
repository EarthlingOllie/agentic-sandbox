"""A set's bindings: every hostname, its leased loopback IP and its native ports.

:func:`lease_ips` is the ``up`` validation step that leases them (``sandbox.leases``). It is
the one validation step that writes host state. Running it before anything starts means an
exhausted pool fails ``up`` cleanly, and the no-op path can still print bindings.

A lease is sticky: once the set has a plan, only ``prune`` releases it. Until then the set
looks orphaned, so ``up`` holds the set's :func:`up_lock_path` throughout (bare ``prune``
skips a set whose lock is held) and releases a lease it created itself if it fails before
the plan exists (see ``Provisioner.up``).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

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


def up_lock_path(config: Config, set_name: str) -> Path:
    """Held by ``up`` of ``set_name`` while it runs, so bare ``prune`` leaves the set alone."""
    return config.lock_dir / f"up-{set_name}.lock"


async def lease_ips(ctx: UpContext) -> None:
    """Lease the set's /24 and one IP per service hostname (kept if already leased)."""
    if not ctx.graph.services:
        return
    hostnames = [hostname(name, ctx.set_name) for name in ctx.graph.names()]
    store = lease_store(ctx.config, ctx.alias_blocks)
    # Under the set's up lock, so no other process creates this set's lease meanwhile.
    existed = store.read(ctx.set_name) is not None
    ctx.lease = await store.lease(ctx.set_name, hostnames)
    ctx.lease_created = not existed


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
