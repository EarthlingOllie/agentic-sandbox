"""``prune``: clear a set's container infrastructure and give its IP lease back to the pool.

Pruning a set removes its Compose project (containers, network, volumes), its hosts block
and its lease. The set directory, plan and worktrees are never touched: ``destroy`` removes
those. A set with a plan and anything running is refused (``down`` it first).

Bare ``prune`` asks the status reconciliation (``sandbox.status``) for every orphaned set
(containers or a lease left behind without a plan, e.g. by a crash) and prunes each,
concurrently under :data:`MAX_CONCURRENT` and independently: one failure is reported and
the rest carry on. A set whose pruning failed keeps its lease, so a rerun retries it.
"""

import asyncio
from dataclasses import dataclass
from typing import Final

from sandbox.bindings import lease_store
from sandbox.compose import project_name
from sandbox.compose.cli import Compose
from sandbox.config import Config
from sandbox.docker import Docker
from sandbox.errors import SandboxError
from sandbox.git import Git
from sandbox.hosts import HostsWriter, hosts_writer
from sandbox.models.status import SetStateName, SetSummary
from sandbox.names import validate_set_name
from sandbox.plan import plan_path
from sandbox.status import StatusReader

MAX_CONCURRENT: Final = 4
# States in which some of the set's services are still running.
_ACTIVE: Final = frozenset({SetStateName.RUNNING, SetStateName.PARTIAL})


@dataclass(frozen=True)
class Pruned:
    set_name: str
    project: str


@dataclass(frozen=True)
class PruneFailure:
    set_name: str
    error: str


@dataclass(frozen=True)
class PruneReport:
    pruned: list[Pruned]
    failed: list[PruneFailure]


class Pruner:
    def __init__(
        self,
        config: Config,
        git: Git,
        compose: Compose,
        docker: Docker,
        hosts: HostsWriter | None = None,
    ) -> None:
        self._config = config
        self._compose = compose
        self._hosts = hosts if hosts is not None else hosts_writer(config)
        self._status = StatusReader(config, git, docker)

    async def prune_set(self, set_name: str) -> Pruned:
        """Prune one set, refusing while any of its services runs."""
        validate_set_name(set_name)
        summary = await self._summary(set_name)
        if summary is None:
            plan_file = plan_path(self._config.sets_root / set_name)
            raise SandboxError(f"no set {set_name!r}: {plan_file} does not exist")
        if summary.state in _ACTIVE:
            raise SandboxError(
                f"set {set_name!r} is {summary.state}; stop it first with: sandbox {set_name} down"
            )
        return await self._clear(set_name)

    async def prune_orphans(self) -> PruneReport:
        """Prune every orphaned set, concurrently under a bound and independently."""
        listing = await self._status.list_sets()
        orphans = [s.name for s in listing.sets if s.state is SetStateName.ORPHANED]
        limit = asyncio.Semaphore(MAX_CONCURRENT)

        async def one(set_name: str) -> Pruned | PruneFailure:
            async with limit:
                try:
                    return await self._clear(set_name)
                except SandboxError as exc:
                    return PruneFailure(set_name, str(exc))

        results = await asyncio.gather(*(one(name) for name in orphans))
        return PruneReport(
            pruned=[r for r in results if isinstance(r, Pruned)],
            failed=[r for r in results if isinstance(r, PruneFailure)],
        )

    async def _summary(self, set_name: str) -> SetSummary | None:
        listing = await self._status.list_sets()
        return next((s for s in listing.sets if s.name == set_name), None)

    async def _clear(self, set_name: str) -> Pruned:
        # Lease last: if anything before it fails, the set stays visible to `status`
        # (and to the next bare `prune`) through its lease.
        project = project_name(set_name)
        await self._compose.down(project)
        await self._hosts.remove(set_name)
        await lease_store(self._config).release(set_name)
        return Pruned(set_name, project)
