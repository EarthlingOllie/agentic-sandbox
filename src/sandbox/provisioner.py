"""Provisioner: orchestrates ``up`` for one set.

``up`` resolves the plan, validates every repo (including its compose file) before touching
the filesystem, writes the plan (never overwriting one), ensures one worktree per repo, then
renders the set's Compose override and brings the repos' backing services up healthy as
Compose project ``sandbox-<set>``. ``down`` stops that project and leaves worktrees alone.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sandbox.compose import project_name
from sandbox.compose.cli import Compose
from sandbox.compose.override import OverrideContext, write_override
from sandbox.compose.services import SANDBOX_DIRNAME, RepoCompose, load_repo_compose
from sandbox.config import Config
from sandbox.errors import SandboxError
from sandbox.git import Git
from sandbox.names import agent_branch, validate_repo_name, validate_set_name
from sandbox.plan import SetPlan, SetRepo, load_set_plan, plan_path, write_new_plan
from sandbox.worktrees import WorktreeManager, WorktreeOutcome, WorktreeResult

Warn = Callable[[str], None]


@dataclass(frozen=True)
class UpResult:
    set_name: str
    set_dir: Path
    plan_file: Path
    plan_created: bool
    worktrees: list[WorktreeResult]
    project: str
    # Service names started under `project`; empty for a worktree-only set.
    services: list[str]


@dataclass(frozen=True)
class DownResult:
    set_name: str
    set_dir: Path
    project: str


class Provisioner:
    def __init__(self, config: Config, git: Git, compose: Compose, warn: Warn) -> None:
        self._config = config
        self._git = git
        self._compose = compose
        self._warn = warn
        self._worktrees = WorktreeManager(git, config.lock_dir)

    def set_dir(self, set_name: str) -> Path:
        return self._config.sets_root / set_name

    async def up(self, set_name: str, *, repos: Sequence[str] | None, cwd: Path) -> UpResult:
        validate_set_name(set_name)
        set_dir = self.set_dir(set_name)
        plan_file = plan_path(set_dir)
        branch = agent_branch(set_name)

        requested = await self._plan_from_directory(cwd, repos) if repos else None
        on_disk = load_set_plan(set_dir) if plan_file.exists() else None
        if on_disk is not None and requested is not None:
            _refuse_if_different(set_name, plan_file, requested, on_disk)
        plan = on_disk or requested
        if plan is None:
            raise SandboxError(
                f"set {set_name!r} has no plan at {plan_file}; "
                f"create it with: sandbox {set_name} up --repos NAME..."
            )

        # Everything that can fail cheaply is checked before anything is created.
        await self._validate_repos(plan, branch)
        repo_composes = _load_composes(plan)

        set_dir.mkdir(parents=True, exist_ok=True)
        plan_created = False
        if on_disk is None:
            try:
                write_new_plan(plan_file, plan)
                plan_created = True
            except SandboxError:
                # Another process created this set first: its plan wins if it matches ours.
                on_disk = load_set_plan(set_dir)
                _refuse_if_different(set_name, plan_file, plan, on_disk)
                plan = on_disk

        worktrees = await self._ensure_worktrees(plan, set_dir, branch)
        for result in worktrees:
            if result.outcome is WorktreeOutcome.REUSED_BRANCH:
                self._warn(
                    f"{result.repo}: reusing existing branch {result.branch}; "
                    f"base {result.base!r} from the plan was ignored"
                )

        project = project_name(set_name)
        services = sorted({service for rc in repo_composes for service in rc.services})
        if services:
            override = write_override(OverrideContext(set_name, set_dir, repo_composes))
            await self._compose.up(project, [*(rc.file for rc in repo_composes), override])
        return UpResult(set_name, set_dir, plan_file, plan_created, worktrees, project, services)

    async def down(self, set_name: str) -> DownResult:
        """Stop the set's containers and remove its network and volumes; keep worktrees."""
        validate_set_name(set_name)
        set_dir = self.set_dir(set_name)
        if not plan_path(set_dir).exists():
            raise SandboxError(f"no set {set_name!r}: {plan_path(set_dir)} does not exist")
        project = project_name(set_name)
        await self._compose.down(project)
        return DownResult(set_name, set_dir, project)

    async def _plan_from_directory(self, cwd: Path, names: Sequence[str]) -> SetPlan:
        """Resolve ``--repos`` names against ``cwd``, pinning absolute sources and bases."""
        errors: list[str] = []
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            errors.append(f"repo(s) named more than once: {', '.join(duplicates)}")
        repos: dict[str, SetRepo] = {}
        for name in dict.fromkeys(names):
            try:
                validate_repo_name(name)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            source = (cwd / name).resolve()
            problem = await self._source_problem(name, source)
            if problem:
                errors.append(problem)
                continue
            base = await self._git.current_branch(source)
            if base is None:
                errors.append(
                    f"{name}: {source} has a detached HEAD; check out the branch to base "
                    "the worktree on"
                )
                continue
            repos[name] = SetRepo(source=source, base=base)
        if errors:
            raise SandboxError("\n".join(errors))
        return SetPlan(repos=repos)

    async def _source_problem(self, name: str, source: Path) -> str | None:
        if not source.is_dir():
            return f"{name}: no repo directory at {source}"
        if not (source / SANDBOX_DIRNAME).is_dir():
            return (
                f"{name}: {source} has no {SANDBOX_DIRNAME}/ directory; "
                "a repo opts in to sandbox by adding one at its root"
            )
        if not await self._git.is_repository(source):
            return f"{name}: {source} is not the root of a git repository"
        return None

    async def _validate_repos(self, plan: SetPlan, branch: str) -> None:
        errors: list[str] = []
        for name, repo in plan.repos.items():
            problem = await self._source_problem(name, repo.source)
            if problem:
                errors.append(problem)
                continue
            if not await self._git.branch_exists(
                repo.source, branch
            ) and not await self._git.revision_exists(repo.source, repo.base):
                errors.append(f"{name}: base {repo.base!r} does not exist in {repo.source}")
        if errors:
            raise SandboxError("\n".join(errors))

    async def _ensure_worktrees(
        self, plan: SetPlan, set_dir: Path, branch: str
    ) -> list[WorktreeResult]:
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(
                        self._worktrees.ensure(
                            repo=name,
                            source=repo.source,
                            path=set_dir / name,
                            branch=branch,
                            base=repo.base,
                        )
                    )
                    for name, repo in sorted(plan.repos.items())
                ]
        except* SandboxError as group:
            raise SandboxError(_join_messages(group)) from group
        return [task.result() for task in tasks]


def _load_composes(plan: SetPlan) -> list[RepoCompose]:
    """Each repo's compose file in repo-name order; repos without one are worktree-only."""
    errors: list[str] = []
    found: list[RepoCompose] = []
    for name, repo in sorted(plan.repos.items()):
        try:
            repo_compose = load_repo_compose(name, repo.source)
        except SandboxError as exc:
            errors.append(str(exc))
            continue
        if repo_compose is not None:
            found.append(repo_compose)
    if errors:
        raise SandboxError("\n".join(errors))
    return found


def _refuse_if_different(
    set_name: str, plan_file: Path, requested: SetPlan, on_disk: SetPlan
) -> None:
    if requested.sources() == on_disk.sources():
        return
    wanted = ", ".join(f"{n}={s}" for n, s in sorted(requested.sources().items()))
    have = ", ".join(f"{n}={s}" for n, s in sorted(on_disk.sources().items()))
    raise SandboxError(
        f"set {set_name!r} already has a different plan at {plan_file}\n"
        f"  requested: {wanted}\n"
        f"  on disk:   {have}\n"
        "the plan on disk wins: edit it to change the set, or run `up` without --repos"
    )


def _join_messages(group: BaseExceptionGroup[SandboxError]) -> str:
    messages: list[str] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            messages.append(_join_messages(exc))  # pyright: ignore[reportUnknownArgumentType]
        else:
            messages.append(str(exc))
    return "\n".join(messages)
