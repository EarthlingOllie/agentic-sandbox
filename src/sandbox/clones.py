"""CloneManager: the agent's own clone of each repo in the set directory, on ``agent/<set>``,
and the sync that brings that branch back into the developer's repo.

The clone borrows the source repo's objects (``git clone --shared``); the agent commits,
branches and fetches there, and never writes the source. ``agent/<set>`` in the source repo
is not live: :meth:`CloneManager.sync` (run by ``up``, ``status``, ``down`` and
``destroy``) fetches the agent's branch into it.
"""

import asyncio
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sandbox.git import SYNCED, Git, SyncResult
from sandbox.locking import file_lock
from sandbox.plan import SetPlan


class CloneOutcome(StrEnum):
    CREATED = "created"
    REUSED_BRANCH = "reused-branch"
    EXISTING = "existing"


@dataclass(frozen=True)
class CloneResult:
    repo: str
    path: Path
    branch: str
    base: str
    outcome: CloneOutcome


@dataclass(frozen=True)
class RepoSync:
    repo: str
    source: Path
    result: SyncResult

    @property
    def synced(self) -> bool:
        return self.result.outcome in SYNCED

    def note(self) -> str:
        """One line for a sync that left the source's branch behind the agent's."""
        return f"{self.repo}: not synced ({self.result.outcome}): {self.result.detail}"


def source_lock_path(lock_dir: Path, source: Path) -> Path:
    """One lock per source repo, shared by every sandbox process on this machine."""
    digest = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    return lock_dir / f"source-{digest}.lock"


class CloneManager:
    def __init__(self, git: Git, lock_dir: Path) -> None:
        self._git = git
        self._lock_dir = lock_dir

    def lock_path(self, source: Path) -> Path:
        return source_lock_path(self._lock_dir, source)

    async def ensure(
        self, *, repo: str, source: Path, path: Path, branch: str, base: str
    ) -> CloneResult:
        """Create the clone unless it already exists.

        A surviving ``branch`` in the source is checked out as-is (``base`` is ignored) so
        earlier agent work is picked up rather than lost. Runs under the per-source-repo
        lock, so a concurrent ``destroy --branches`` can't delete the branch half-way.
        """
        if (path / ".git").exists():
            return CloneResult(repo, path, branch, base, CloneOutcome.EXISTING)
        async with file_lock(self.lock_path(source)):
            if await self._git.branch_exists(source, branch):
                await self._git.clone_for_agent(source, path, branch, base=None)
                outcome = CloneOutcome.REUSED_BRANCH
            else:
                await self._git.clone_for_agent(source, path, branch, base=base)
                outcome = CloneOutcome.CREATED
        return CloneResult(repo, path, branch, base, outcome)

    async def sync(self, *, repo: str, source: Path, path: Path, branch: str) -> RepoSync:
        """Fetch the clone's ``branch`` into the source's (it moves the source's refs, so
        under the per-source-repo lock)."""
        async with file_lock(self.lock_path(source)):
            result = await self._git.sync_agent_branch(source, path, branch)
        return RepoSync(repo, source, result)

    async def sync_set(self, set_dir: Path, plan: SetPlan, branch: str) -> list[RepoSync]:
        """Sync every repo of the set, in repo-name order."""
        names = sorted(plan.repos)
        return list(
            await asyncio.gather(
                *(
                    self.sync(
                        repo=name, source=plan.repos[name].source, path=set_dir / name,
                        branch=branch,
                    )
                    for name in names
                )
            )
        )  # fmt: skip
