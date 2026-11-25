"""Set states for ``status``: set directories reconciled against Docker labels.

Two sources describe sets, and neither alone is the truth:

- the sets root: a set directory with a plan (``sandbox.yaml``) is a set;
- Docker: every container labelled ``sandbox.set=<set>`` (``sandbox.docker``), and the
  address of each running one. Nothing else records where a service is, so a stopped
  service has a set hostname and no address.

A set with a plan is **running** when every service it expects (its repos' services and
the agent) has a running container, **stopped** when none does and **partial** in between.
Anything else is **orphaned**: containers whose set has no plan, or a set directory that
never got one. ``prune`` clears orphans. A lease file left by the loopback model is not
read: nothing leases IPs any more, so nothing could clear an orphan it described.

One set's status first syncs each agent clone's ``agent/<set>`` into its source repo
(``sandbox.clones``), then reports each clone's branch and changes.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

from sandbox.agent import set_services
from sandbox.clones import CloneManager
from sandbox.compose import AGENT_SERVICE, project_name
from sandbox.compose.network import hostname
from sandbox.config import Config
from sandbox.docker import Container, Docker
from sandbox.errors import SandboxError
from sandbox.git import Git
from sandbox.models.status import (
    RepoStatus,
    ServiceStatus,
    SetStateName,
    SetStatus,
    SetSummary,
    StatusList,
)
from sandbox.names import agent_branch, validate_set_name
from sandbox.plan import SetPlan, load_set_plan, plan_path

RUNNING = "running"
MISSING = "missing"


@dataclass(frozen=True)
class _Observed:
    """One snapshot of the two sources, keyed by set name."""

    set_dirs: set[str]
    containers: dict[str, list[Container]]

    def names(self) -> list[str]:
        return sorted(self.set_dirs | self.containers.keys())


class StatusReader:
    def __init__(self, config: Config, git: Git, docker: Docker) -> None:
        self._config = config
        self._git = git
        self._docker = docker
        self._clones = CloneManager(git, config.lock_dir)

    async def list_sets(self) -> StatusList:
        observed = await self._observe()
        return StatusList(sets=[self._summary(name, observed) for name in observed.names()])

    async def set_status(self, set_name: str) -> SetStatus:
        validate_set_name(set_name)
        observed = await self._observe()
        if set_name not in observed.names():
            plan_file = plan_path(self._set_dir(set_name))
            raise SandboxError(f"no set {set_name!r}: {plan_file} does not exist")
        summary = self._summary(set_name, observed)
        plan = self._plan(set_name)
        containers = observed.containers.get(set_name, [])
        expected = self._expected(plan) if plan is not None else set[str]()
        names = sorted(expected | {c.service for c in containers})
        domain = self._config.domain
        services = [_service(set_name, domain, name, containers) for name in names]
        repos = await self._repos(set_name, plan) if plan is not None else []
        return SetStatus(
            name=set_name,
            state=summary.state,
            detail=summary.detail,
            project=project_name(set_name),
            services=services,
            repos=repos,
        )

    async def addresses(self, set_name: str) -> tuple[SetStateName, dict[str, IPv4Address]]:
        """The set's state and each running service's current address, from one snapshot."""
        observed = await self._observe()
        if set_name not in observed.names():
            # No plan and nothing left of it in Docker: nothing runs.
            return SetStateName.STOPPED, {}
        found: dict[str, IPv4Address] = {}
        for container in observed.containers.get(set_name, []):
            if container.state == RUNNING and container.address is not None:
                found.setdefault(container.service, container.address)
        return self._summary(set_name, observed).state, found

    def _set_dir(self, set_name: str) -> Path:
        return self._config.sets_root / set_name

    def _plan(self, set_name: str) -> SetPlan | None:
        set_dir = self._set_dir(set_name)
        return load_set_plan(set_dir) if plan_path(set_dir).exists() else None

    async def _observe(self) -> _Observed:
        containers: defaultdict[str, list[Container]] = defaultdict(list)
        for container in await self._docker.set_containers():
            containers[container.set_name].append(container)
        return _Observed(
            set_dirs=self._set_dirs(),
            containers=dict(containers),
        )

    def _set_dirs(self) -> set[str]:
        root = self._config.sets_root
        if not root.is_dir():
            return set()
        found: set[str] = set()
        for path in root.iterdir():
            if not path.is_dir():
                continue
            try:
                found.add(validate_set_name(path.name))
            except SandboxError:
                continue  # not a set sandbox could have created
        return found

    def _expected(self, plan: SetPlan) -> set[str]:
        try:
            return set(set_services(plan))
        except SandboxError:
            # A repo's compose file is unreadable now; judge by the agent alone.
            return {AGENT_SERVICE}

    def _summary(self, set_name: str, observed: _Observed) -> SetSummary:
        containers = observed.containers.get(set_name, [])
        plan = self._plan(set_name)
        if plan is None:
            return SetSummary(
                name=set_name,
                state=SetStateName.ORPHANED,
                detail=_orphan_detail(
                    has_dir=set_name in observed.set_dirs,
                    containers=len(containers),
                ),
            )
        running = {c.service for c in containers if c.state == RUNNING}
        expected = self._expected(plan)
        if expected <= running:
            return SetSummary(name=set_name, state=SetStateName.RUNNING)
        if not running:
            return SetSummary(name=set_name, state=SetStateName.STOPPED)
        return SetSummary(
            name=set_name,
            state=SetStateName.PARTIAL,
            detail=f"not running: {', '.join(sorted(expected - running))}",
        )

    async def _repos(self, set_name: str, plan: SetPlan) -> list[RepoStatus]:
        set_dir = self._set_dir(set_name)
        synced = await self._clones.sync_set(set_dir, plan, agent_branch(set_name))
        names = sorted(plan.repos)
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(self._git.clone_status(plan.repos[n].source, set_dir / n))
                for n in names
            ]
        repos: list[RepoStatus] = []
        for name, task, sync in zip(names, tasks, synced, strict=True):
            found = task.result()
            repos.append(
                RepoStatus(
                    name=name,
                    path=set_dir / name,
                    exists=found is not None,
                    branch=found.branch if found else None,
                    modified=found.modified if found else False,
                    untracked=found.untracked if found else False,
                    sync=sync.result.outcome,
                    sync_detail=sync.result.detail,
                )
            )
        return repos


def _service(
    set_name: str, domain: str, service: str, containers: list[Container]
) -> ServiceStatus:
    own = [c for c in containers if c.service == service]
    # With several containers for one service, a running one speaks for it.
    container = next((c for c in own if c.state == RUNNING), own[0] if own else None)
    running = container is not None and container.state == RUNNING
    return ServiceStatus(
        name=service,
        state=container.state if container else MISSING,
        health=container.health if container and running else None,
        hostname=hostname(service, set_name, domain),
        ip=container.address if container and running else None,
    )


def _orphan_detail(*, has_dir: bool, containers: int) -> str:
    if not containers:
        return "set directory without a plan"
    where = "a set directory without a plan" if has_dir else "no set directory"
    return f"{containers} container{'s' if containers != 1 else ''} with {where}"
