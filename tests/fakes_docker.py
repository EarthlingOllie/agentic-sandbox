"""In-memory stand-in for the ``docker`` label queries (``sandbox.docker.Docker``).

By default it reports the containers a :class:`~tests.fakes_compose.FakeCompose` is
running: one per service of each project's files, labelled with the set, ``exited`` when
the test stopped it. Tests add leftovers (orphans), health and the addresses Docker gave
the containers through its fields; like Docker, it reports an address only for a running
container.

The resolver is a domain (while it runs) and the set of networks it is attached to. The
networks live on the Compose fake, whose `down` refuses to remove a network the resolver is
still on, as Docker does.
"""

import shutil
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path

from sandbox.compose import PROJECT_PREFIX
from sandbox.docker import Container, DockerError
from tests.fakes_compose import FakeCompose, load_compose_yaml


@dataclass
class FakeDocker:
    compose: FakeCompose = field(default_factory=FakeCompose)
    # Containers that exist besides the ones `compose` is running (e.g. crash leftovers).
    extra: list[Container] = field(default_factory=list[Container])
    # (set, service) -> health status reported for that service's running container
    health: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    # (set, service) -> the address of that service's container on the set network
    addresses: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    # The directories `empty_as_root` emptied, in call order.
    emptied: list[Path] = field(default_factory=list[Path])
    # The domain the running resolver forwards; None while it is not running.
    resolver_domain: str | None = None
    # How many times the resolver was started (or restarted for another domain).
    resolver_starts: int = 0
    # When set, starting the resolver fails with this message.
    resolver_error: str | None = None
    # Who each removal of the resolver ran as (None: whoever runs sandbox), in call order.
    resolver_removals: list[str | None] = field(default_factory=list[str | None])
    # When set, removing the resolver fails with this message.
    resolver_removal_error: str | None = None

    @property
    def resolver_networks(self) -> set[str]:
        """The networks the resolver is attached to."""
        return self.compose.resolver_networks

    async def ensure_resolver(self, domain: str) -> None:
        if self.resolver_error is not None:
            raise DockerError(self.resolver_error)
        if self.resolver_domain != domain:
            self.resolver_starts += 1
        self.resolver_domain = domain

    async def attach_resolver(self, network: str) -> None:
        if self.resolver_domain is None:
            raise DockerError("the resolver is not running")
        self.compose.resolver_networks.add(network)

    async def detach_resolver(self, network: str) -> None:
        self.compose.resolver_networks.discard(network)

    async def remove_resolver(self, as_user: str | None = None) -> None:
        if self.resolver_removal_error is not None:
            raise DockerError(self.resolver_removal_error)
        self.resolver_removals.append(as_user)
        self.resolver_domain = None
        self.compose.resolver_networks.clear()

    async def set_containers(self) -> list[Container]:
        found: list[Container] = []
        for project, files in self.compose.running.items():
            set_name = project.removeprefix(PROJECT_PREFIX)
            stopped = self.compose.stopped_services.get(project, set())
            services: set[str] = set()
            for path in files:
                services.update(load_compose_yaml(path).get("services") or {})
            for service in sorted(services):
                state = "exited" if service in stopped else "running"
                running = state == "running"
                health = self.health.get((set_name, service)) if running else None
                address = self.addresses.get((set_name, service)) if running else None
                found.append(
                    Container(
                        set_name, service, state, health, IPv4Address(address) if address else None
                    )
                )
        return [*found, *self.extra]

    async def empty_as_root(self, path: Path) -> None:
        self.emptied.append(path)
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
