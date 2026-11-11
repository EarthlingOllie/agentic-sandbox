"""In-memory stand-ins for external tools, used by unit tests behind the CLI seam."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from sandbox.git import GitCommandError, WorktreeStatus


@dataclass
class FakeRepo:
    current: str | None = "main"
    branches: set[str] = field(default_factory=lambda: {"main"})
    # branch -> worktree path, for branches checked out in a linked worktree
    checked_out: dict[str, Path] = field(default_factory=dict[str, Path])


@dataclass
class FakeWorktree:
    branch: str | None
    modified: bool = False
    untracked: bool = False


class FakeGit:
    """Implements the ``sandbox.git.Git`` protocol against in-memory repos.

    ``add_worktree`` creates the worktree directory (with a ``.git`` file, like real git)
    so filesystem-level assertions work the same as in integration runs.
    """

    def __init__(self, add_delay: float = 0.0) -> None:
        self.repos: dict[Path, FakeRepo] = {}
        self.add_delay = add_delay
        self.active_adds: dict[Path, int] = {}
        self.max_concurrent_adds: dict[Path, int] = {}
        self.worktrees: dict[Path, FakeWorktree] = {}

    def add_repo(
        self, path: Path, *, current: str | None = "main", branches: set[str] | None = None
    ) -> FakeRepo:
        repo = FakeRepo(current=current, branches=branches or {"main"})
        if current is not None:
            repo.branches.add(current)
        self.repos[path.resolve()] = repo
        return repo

    def worktree(self, path: Path) -> FakeWorktree:
        return self.worktrees[path.resolve()]

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

    async def prune_worktrees(self, repo: Path) -> None:
        state = self.repo(repo)
        state.checked_out = {b: p for b, p in state.checked_out.items() if (p / ".git").exists()}

    async def add_worktree(self, repo: Path, path: Path, branch: str, *, base: str | None) -> None:
        key = repo.resolve()
        state = self.repo(repo)
        self.active_adds[key] = self.active_adds.get(key, 0) + 1
        self.max_concurrent_adds[key] = max(
            self.max_concurrent_adds.get(key, 0), self.active_adds[key]
        )
        try:
            await asyncio.sleep(self.add_delay)
            if base is None:
                if branch not in state.branches:
                    raise GitCommandError(f"fatal: invalid reference: {branch}")
            else:
                if branch in state.branches:
                    raise GitCommandError(f"fatal: a branch named '{branch}' already exists")
                if base not in state.branches:
                    raise GitCommandError(f"fatal: invalid reference: {base}")
                state.branches.add(branch)
            if branch in state.checked_out:
                raise GitCommandError(f"fatal: '{branch}' is already checked out")
            path.mkdir(parents=True, exist_ok=True)
            (path / ".git").write_text(f"gitdir: {key}/.git/worktrees/{path.name}\n")
            state.checked_out[branch] = path
            self.worktrees[path.resolve()] = FakeWorktree(branch)
        finally:
            self.active_adds[key] -= 1

    async def worktree_status(self, path: Path) -> WorktreeStatus | None:
        worktree = self.worktrees.get(path.resolve())
        if worktree is None or not (path / ".git").exists():
            return None
        return WorktreeStatus(worktree.branch, worktree.modified, worktree.untracked)
