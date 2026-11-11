"""In-memory stand-in for the ``docker`` label queries (``sandbox.docker.Docker``).

By default it reports the containers a :class:`~tests.fakes_compose.FakeCompose` is
running: one per service of each project's files, labelled with the set, ``exited`` when
the test stopped it. Tests add leftovers (orphans) and health through its fields.
"""

from dataclasses import dataclass, field

from sandbox.compose import PROJECT_PREFIX
from sandbox.docker import Container
from tests.fakes_compose import FakeCompose, load_compose_yaml


@dataclass
class FakeDocker:
    compose: FakeCompose = field(default_factory=FakeCompose)
    # Containers that exist besides the ones `compose` is running (e.g. crash leftovers).
    extra: list[Container] = field(default_factory=list[Container])
    # (set, service) -> health status reported for that service's running container
    health: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])

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
                health = self.health.get((set_name, service)) if state == "running" else None
                found.append(Container(set_name, service, state, health))
        return [*found, *self.extra]
