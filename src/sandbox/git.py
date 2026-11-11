"""The git operations sandbox needs, behind a small protocol so unit tests can use a fake."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sandbox.errors import SandboxError


@dataclass(frozen=True)
class WorktreeStatus:
    branch: str | None  # None on a detached HEAD
    modified: bool  # staged or unstaged changes to tracked files (or conflicts)
    untracked: bool  # files git doesn't track and doesn't ignore
    # The paths behind ``modified`` and ``untracked``, as ``git status`` names them.
    changes: tuple[str, ...] = ()


class Git(Protocol):
    async def is_repository(self, path: Path) -> bool: ...

    async def current_branch(self, repo: Path) -> str | None:
        """The checked-out branch, or None on a detached HEAD."""
        ...

    async def branch_exists(self, repo: Path, branch: str) -> bool: ...

    async def revision_exists(self, repo: Path, revision: str) -> bool: ...

    async def prune_worktrees(self, repo: Path) -> None: ...

    async def add_worktree(self, repo: Path, path: Path, branch: str, *, base: str | None) -> None:
        """Add a worktree at ``path`` on ``branch``.

        With ``base``, the branch is created from it; without, the existing branch is used.
        """
        ...

    async def worktree_status(self, path: Path) -> WorktreeStatus | None:
        """The worktree's branch and whether it has uncommitted or untracked changes, or
        None when ``path`` is not a worktree (e.g. it was deleted)."""
        ...

    async def remove_worktree(self, repo: Path, path: Path, *, force: bool) -> None:
        """Remove the worktree at ``path`` and its metadata in ``repo``; without ``force``
        git refuses a worktree with uncommitted or untracked changes."""
        ...

    async def unpushed_commits(self, repo: Path, branch: str) -> list[str]:
        """The commits (``<short sha> <subject>``) only ``branch`` holds: on no remote-tracking
        branch, other local branch or tag, so deleting ``branch`` would lose them."""
        ...

    async def delete_branch(self, repo: Path, branch: str) -> None: ...


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class GitCommandError(SandboxError):
    pass


class SubprocessGit:
    """Runs the real ``git`` binary."""

    async def _run(self, repo: Path, *args: str) -> GitResult:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo),
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SandboxError("git is not installed or not on PATH") from exc
        stdout, stderr = await process.communicate()
        return GitResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace").strip(),
            stderr=stderr.decode(errors="replace").strip(),
        )

    async def _check(self, repo: Path, *args: str) -> str:
        result = await self._run(repo, *args)
        if result.returncode != 0:
            raise GitCommandError(
                f"git {' '.join(args)} failed in {repo}: {result.stderr or result.stdout}"
            )
        return result.stdout

    async def is_repository(self, path: Path) -> bool:
        result = await self._run(path, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            return False
        return Path(result.stdout).resolve() == path.resolve()

    async def current_branch(self, repo: Path) -> str | None:
        result = await self._run(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        return result.stdout if result.returncode == 0 and result.stdout else None

    async def branch_exists(self, repo: Path, branch: str) -> bool:
        result = await self._run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        return result.returncode == 0

    async def revision_exists(self, repo: Path, revision: str) -> bool:
        result = await self._run(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
        return result.returncode == 0

    async def prune_worktrees(self, repo: Path) -> None:
        await self._check(repo, "worktree", "prune")

    async def add_worktree(self, repo: Path, path: Path, branch: str, *, base: str | None) -> None:
        if base is None:
            await self._check(repo, "worktree", "add", str(path), branch)
        else:
            await self._check(repo, "worktree", "add", "-b", branch, str(path), base)

    async def worktree_status(self, path: Path) -> WorktreeStatus | None:
        if not path.is_dir():
            return None
        result = await self._run(path, "status", "--porcelain=v2", "--branch")
        if result.returncode != 0:
            return None
        branch: str | None = None
        modified = untracked = False
        changes: list[str] = []
        for line in result.stdout.splitlines():
            if line.startswith("# branch.head "):
                head = line.removeprefix("# branch.head ")
                branch = None if head == "(detached)" else head
            elif line.startswith(("1 ", "2 ", "u ")):
                modified = True
                # Ordinary (1), renamed (2: "<path>\t<original>") and unmerged (u) entries
                # carry 8, 9 and 10 space-separated fields before the path.
                fields = {"1": 8, "2": 9, "u": 10}[line[0]]
                changes.append(line.split(" ", fields)[-1].split("\t")[0])
            elif line.startswith("? "):
                untracked = True
                changes.append(line.removeprefix("? "))
        return WorktreeStatus(branch, modified, untracked, tuple(changes))

    async def remove_worktree(self, repo: Path, path: Path, *, force: bool) -> None:
        await self._check(repo, "worktree", "remove", *(["--force"] if force else []), str(path))

    async def unpushed_commits(self, repo: Path, branch: str) -> list[str]:
        ref = f"refs/heads/{branch}"
        output = await self._check(
            repo,
            "log",
            "--format=%h %s",
            ref,
            "--not",
            # Git versions differ on whether --branches matches --exclude against the full
            # ref or the name below refs/heads/; excluding both is right on either.
            f"--exclude={ref}",
            f"--exclude={branch}",
            "--branches",
            "--remotes",
            "--tags",
            "--",
        )
        return output.splitlines()

    async def delete_branch(self, repo: Path, branch: str) -> None:
        await self._check(repo, "branch", "--delete", "--force", branch)
