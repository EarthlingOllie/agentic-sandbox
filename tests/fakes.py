"""In-memory stand-ins for external tools, used by unit tests behind the CLI seam."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from sandbox.git import CloneStatus, GitCommandError, SyncOutcome, SyncResult

# A file by this name stands for root-owned files a container left in a directory; see
# tests/unit/test_destroy.py.
ROOT_OWNED_MARKER = ".root-owned"


@dataclass
class FakeRepo:
    current: str | None = "main"
    branches: set[str] = field(default_factory=lambda: {"main"})
    # branch -> the commits (subjects, oldest first) the branch holds beyond its base
    commits: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    # branch -> the commits (``<sha> <subject>``) only that branch holds
    unpushed: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    # branch -> the agent's latest synced tip of it (``sandbox.git.agent_tip_ref``)
    agent_tips: dict[str, list[str]] = field(default_factory=dict[str, list[str]])


@dataclass
class FakeClone:
    """The agent's clone of a repo: its branch and the agent's commits on it."""

    branch: str | None
    commits: list[str] = field(default_factory=list[str])
    modified: bool = False
    untracked: bool = False
    changes: tuple[str, ...] = ()
    # What syncing it reports instead of syncing (e.g. a diverged branch).
    sync_result: SyncResult | None = None


class FakeGit:
    """Implements the ``sandbox.git.Git`` protocol against in-memory repos.

    ``clone_for_agent`` creates the clone's directory (with a ``.git`` directory, like real
    git) so filesystem-level assertions work the same as in integration runs; the agent's
    commits are :attr:`FakeClone.commits`, which syncing copies into the source repo.
    """

    def __init__(self, add_delay: float = 0.0) -> None:
        self.repos: dict[Path, FakeRepo] = {}
        self.add_delay = add_delay
        self.active_adds: dict[Path, int] = {}
        self.max_concurrent_adds: dict[Path, int] = {}
        self.clones: dict[Path, FakeClone] = {}
        # (source, clone) per sync, in call order.
        self.syncs: list[tuple[Path, Path]] = []

    def add_repo(
        self, path: Path, *, current: str | None = "main", branches: set[str] | None = None
    ) -> FakeRepo:
        repo = FakeRepo(current=current, branches=branches or {"main"})
        if current is not None:
            repo.branches.add(current)
        self.repos[path.resolve()] = repo
        return repo

    def clone(self, path: Path) -> FakeClone:
        return self.clones[path.resolve()]

    def repo(self, path: Path) -> FakeRepo:
        try:
            return self.repos[path.resolve()]
        except KeyError:
            raise GitCommandError(f"fatal: not a git repository: {path}") from None

    async def is_repository(self, path: Path) -> bool:
        return path.resolve() in self.repos

    async def current_branch(self, repo: Path) -> str | None:
        return self.repo(repo).current

    async def branch_exists(self, repo: Path, branch: str) -> bool:
        return branch in self.repo(repo).branches

    async def revision_exists(self, repo: Path, revision: str) -> bool:
        return revision in self.repo(repo).branches

    async def clone_for_agent(
        self, repo: Path, path: Path, branch: str, *, base: str | None
    ) -> None:
        key = repo.resolve()
        state = self.repo(repo)
        self.active_adds[key] = self.active_adds.get(key, 0) + 1
        self.max_concurrent_adds[key] = max(
            self.max_concurrent_adds.get(key, 0), self.active_adds[key]
        )
        try:
            await asyncio.sleep(self.add_delay)
            start = base if base is not None else branch
            if start not in state.branches:
                raise GitCommandError(f"fatal: invalid reference: {start}")
            (path / ".git").mkdir(parents=True)
            self.clones[path.resolve()] = FakeClone(branch, list(state.commits.get(start, [])))
        finally:
            self.active_adds[key] -= 1

    def _clone_at(self, path: Path) -> FakeClone | None:
        clone = self.clones.get(path.resolve())
        return clone if clone is not None and (path / ".git").is_dir() else None

    async def clone_status(self, repo: Path, path: Path) -> CloneStatus | None:
        clone = self._clone_at(path)
        if clone is None:
            return None
        return CloneStatus(clone.branch, clone.modified, clone.untracked, clone.changes)

    async def sync_agent_branch(self, repo: Path, path: Path, branch: str) -> SyncResult:
        self.syncs.append((repo.resolve(), path.resolve()))
        state = self.repo(repo)
        clone = self._clone_at(path)
        if clone is None:
            return SyncResult(SyncOutcome.NO_CLONE)
        if clone.sync_result is not None:
            return clone.sync_result
        state.agent_tips[branch] = list(clone.commits)
        if branch in state.branches and state.commits.get(branch, []) == clone.commits:
            return SyncResult(SyncOutcome.UNCHANGED)
        state.branches.add(branch)
        state.commits[branch] = list(clone.commits)
        return SyncResult(SyncOutcome.UPDATED)

    async def forget_agent_tip(self, repo: Path, branch: str) -> None:
        self.repo(repo).agent_tips.pop(branch, None)

    async def unpushed_commits(self, repo: Path, branch: str) -> list[str]:
        return list(self.repo(repo).unpushed.get(branch, []))

    async def delete_branch(self, repo: Path, branch: str) -> None:
        state = self.repo(repo)
        if branch not in state.branches:
            raise GitCommandError(f"error: branch '{branch}' not found")
        state.branches.discard(branch)
        state.commits.pop(branch, None)
