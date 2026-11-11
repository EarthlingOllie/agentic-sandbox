"""``destroy``: remove a set for good, without ever silently losing the agent's work.

Destroying a set prunes it (``sandbox.prune``: containers, network, volumes, hosts block,
IP lease), then removes each worktree through ``git worktree remove`` so the source repos'
worktree metadata stays clean, then deletes the set directory (plan, override, certificate).

Every check runs before anything is removed, and a refusal leaves the set as it was:

- a set with any service running is refused (``down`` it first), even with ``force``;
- a worktree with uncommitted or untracked changes is refused, naming them, unless ``force``;
- the ``agent/<set>`` branches are kept unless ``branches``; with it, a branch holding
  commits no remote-tracking branch, other branch or tag holds is refused unless ``force``.

Containers write into the worktrees as whatever user they run as, so a worktree can hold
root-owned files the developer can't delete. On Linux those are deleted from a throwaway
root container started with the ``docker`` CLI (sudo is never needed); on macOS, Docker
Desktop maps container files to the developer, so the case doesn't arise.
"""

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sandbox.compose.cli import Compose
from sandbox.config import Config
from sandbox.docker import Docker
from sandbox.errors import SandboxError
from sandbox.git import Git, GitCommandError
from sandbox.hosts import HostsWriter
from sandbox.locking import file_lock
from sandbox.models.status import SetStateName
from sandbox.names import BRANCH_PREFIX, validate_set_name
from sandbox.plan import load_set_plan, plan_path
from sandbox.prune import Pruner
from sandbox.status import StatusReader
from sandbox.worktrees import WorktreeManager

# How many changed paths or commits a refusal names per repo before summarising the rest.
MAX_NAMED: Final = 10
_PERMISSION_DENIED: Final = "Permission denied"
# States in which some of the set's services are still running.
_ACTIVE: Final = frozenset({SetStateName.RUNNING, SetStateName.PARTIAL})


@dataclass(frozen=True)
class Destroyed:
    set_name: str
    set_dir: Path
    branch: str
    worktrees: list[Path]
    # The source repos whose agent branch was deleted (only with ``branches``).
    deleted_branches: list[Path]
    # The source repos whose agent branch was kept.
    kept_branches: list[Path]
    # Directories whose root-owned files a throwaway container deleted.
    root_cleaned: list[Path]


def _named(items: list[str] | tuple[str, ...]) -> str:
    shown = ", ".join(items[:MAX_NAMED])
    rest = len(items) - MAX_NAMED
    return f"{shown} and {rest} more" if rest > 0 else shown


class Destroyer:
    def __init__(
        self,
        config: Config,
        git: Git,
        compose: Compose,
        docker: Docker,
        hosts: HostsWriter | None = None,
    ) -> None:
        self._config = config
        self._git = git
        self._docker = docker
        self._pruner = Pruner(config, git, compose, docker, hosts)
        self._status = StatusReader(config, git, docker)
        self._worktrees = WorktreeManager(git, config.lock_dir)
        self._root_cleaned: list[Path] = []

    async def destroy(self, set_name: str, *, force: bool, branches: bool) -> Destroyed:
        validate_set_name(set_name)
        set_dir = self._config.sets_root / set_name
        if not plan_path(set_dir).exists():
            raise SandboxError(f"no set {set_name!r}: {plan_path(set_dir)} does not exist")
        plan = load_set_plan(set_dir)
        branch = f"{BRANCH_PREFIX}{set_name}"
        worktrees = {name: (repo.source, set_dir / name) for name, repo in plan.repos.items()}

        # Refusals first, so a refused destroy changes nothing. Pruning refuses a set with
        # anything running; its own check is repeated here so that refusal comes first too.
        await self._refuse_running(set_name)
        if not force:
            await self._refuse_dirty(set_name, worktrees)
        if branches and not force:
            await self._refuse_unpushed(set_name, branch, plan.sources())

        await self._pruner.prune_set(set_name)
        for source, path in worktrees.values():
            await self._remove_worktree(source, path, force=force)
        deleted: list[Path] = []
        kept: list[Path] = []
        for source in plan.sources().values():
            if not await self._git.branch_exists(source, branch):
                continue
            if branches:
                async with file_lock(self._worktrees.lock_path(source)):
                    await self._git.delete_branch(source, branch)
                deleted.append(source)
            else:
                kept.append(source)
        await self._remove_tree(set_dir)
        return Destroyed(
            set_name=set_name,
            set_dir=set_dir,
            branch=branch,
            worktrees=[path for _, path in worktrees.values()],
            deleted_branches=deleted,
            kept_branches=kept,
            root_cleaned=list(self._root_cleaned),
        )

    async def _refuse_running(self, set_name: str) -> None:
        listing = await self._status.list_sets()
        summary = next((s for s in listing.sets if s.name == set_name), None)
        if summary is not None and summary.state in _ACTIVE:
            raise SandboxError(
                f"set {set_name!r} is {summary.state}; stop it first with: sandbox {set_name} down"
            )

    async def _refuse_dirty(self, set_name: str, worktrees: dict[str, tuple[Path, Path]]) -> None:
        problems: list[str] = []
        for name, (source, path) in worktrees.items():
            status = await self._git.worktree_status(source, path)
            if status is None or not (status.modified or status.untracked):
                continue
            kinds = [
                kind
                for kind, present in (
                    ("uncommitted changes", status.modified),
                    ("untracked files", status.untracked),
                )
                if present
            ]
            problems.append(f"  {name} ({path}): {' and '.join(kinds)}: {_named(status.changes)}")
        if problems:
            raise SandboxError(
                f"set {set_name!r} has work that destroy would discard:\n"
                + "\n".join(problems)
                + "\ncommit (and push) it, or rerun with --force to discard it"
            )

    async def _refuse_unpushed(self, set_name: str, branch: str, sources: dict[str, Path]) -> None:
        problems: list[str] = []
        for name, source in sources.items():
            if not await self._git.branch_exists(source, branch):
                continue
            commits = await self._git.unpushed_commits(source, branch)
            if commits:
                problems.append(
                    f"  {name} ({source}): {len(commits)} unpushed commit(s): {_named(commits)}"
                )
        if problems:
            raise SandboxError(
                f"set {set_name!r}: --branches would delete {branch} with commits that exist "
                "on no remote, other branch or tag:\n"
                + "\n".join(problems)
                + f"\npush them, drop --branches to keep {branch}, "
                "or rerun with --force to delete them"
            )

    async def _remove_worktree(self, source: Path, path: Path, *, force: bool) -> None:
        async with file_lock(self._worktrees.lock_path(source)):
            if (path / ".git").exists():
                try:
                    await self._git.remove_worktree(source, path, force=force)
                except GitCommandError as exc:
                    if not self._may_clean_as_root(exc):
                        raise
                    await self._empty_as_root(path)
            if path.exists():
                await self._remove_tree(path)
            # Drops the metadata of a worktree that was deleted rather than removed.
            await self._git.prune_worktrees(source)

    async def _remove_tree(self, path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except PermissionError as exc:
            if not self._may_clean_as_root(exc):
                raise SandboxError(f"cannot delete {path}: {exc}") from exc
            await self._empty_as_root(path)
            path.rmdir()

    @staticmethod
    def _may_clean_as_root(exc: Exception) -> bool:
        return sys.platform.startswith("linux") and (
            isinstance(exc, PermissionError) or _PERMISSION_DENIED in str(exc)
        )

    async def _empty_as_root(self, path: Path) -> None:
        await self._docker.empty_as_root(path)
        self._root_cleaned.append(path)
