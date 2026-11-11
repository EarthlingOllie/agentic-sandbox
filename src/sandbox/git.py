"""The git operations sandbox needs, behind a small protocol so unit tests can use a fake."""

import asyncio
import os
import stat
import tempfile
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

    async def worktree_status(self, repo: Path, path: Path) -> WorktreeStatus | None:
        """The branch of ``repo``'s worktree at ``path`` and whether it has uncommitted or
        untracked changes, or None when ``path`` is not one of ``repo``'s worktrees (e.g. it
        was deleted)."""
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


# Host-side git never runs code the agent chose. Two layers, either of which suffices for
# what git reads from config:
#
# * The agent can't write any config host git reads: the agent mounts a source's ``.git``
#   read-only, with only the directories it needs to commit, branch and fetch read-write
#   (see sandbox.compose.agent), and :meth:`SubprocessGit.worktree_status` never reads the
#   one config file in those (a worktree's ``config.worktree``).
# * Every call runs with isolated config: no system or global config, nothing from the
#   environment, and every setting that names a program to run pinned off. A filter's name
#   is the agent's to choose (via ``.gitattributes``), so filters can't be pinned; nor can
#   ``include.path``: they are covered by the first layer.
_PINNED_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
    ("core.sshCommand", "false"),
    ("core.gitProxy", ""),
    ("core.askPass", ""),
    ("core.pager", "cat"),
    ("core.editor", "false"),
    ("core.alternateRefsCommand", ""),
    ("sequence.editor", "false"),
    ("diff.external", ""),
    ("diff.ignoreSubmodules", "all"),
    ("submodule.recurse", "false"),
    ("status.submoduleSummary", "false"),
    ("credential.helper", ""),
    ("protocol.allow", "never"),
    ("log.showSignature", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)

# Git's environment variables name config, a repository or a program; only identity is kept.
_KEPT_GIT_ENV = ("GIT_AUTHOR_", "GIT_COMMITTER_")


def _isolated_environment(env: dict[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key.startswith(_KEPT_GIT_ENV)
    }
    pins = {"GIT_CONFIG_COUNT": str(len(_PINNED_CONFIG))}
    for index, (key, value) in enumerate(_PINNED_CONFIG):
        pins[f"GIT_CONFIG_KEY_{index}"] = key
        pins[f"GIT_CONFIG_VALUE_{index}"] = value
    return {
        **environment,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        **pins,
        **env,
    }


def worktree_git_dir(repo: Path, path: Path) -> Path | None:
    """The git dir of ``repo``'s worktree at ``path``, or None if ``path`` isn't one.

    The agent can rewrite ``path/.git`` (and replace ``path`` with a symlink), so the target
    is only trusted when it is a direct child of ``repo``'s own ``.git/worktrees`` whose
    ``gitdir`` file (which the agent can only write for its own worktrees) points back at
    ``path/.git``.
    """
    link = path / ".git"
    if path.is_symlink() or link.is_symlink() or not link.is_file():
        return None
    text = _read_regular(link)
    if text is None or not text.startswith("gitdir: "):
        return None
    target = Path(os.path.normpath(path / text.strip().removeprefix("gitdir: ")))
    worktrees = (repo / ".git" / "worktrees").resolve()
    if target.parent.resolve() != worktrees or target.name in ("", ".", ".."):
        return None
    git_dir = worktrees / target.name
    if git_dir.is_symlink() or not git_dir.is_dir():
        return None
    backlink = _read_regular(git_dir / "gitdir")
    if backlink is None or Path(backlink.strip()).resolve() != link.resolve():
        return None
    return git_dir


def _read_regular(path: Path) -> str | None:
    data = _read_regular_bytes(path)
    if data is None:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


def _read_regular_bytes(path: Path) -> bytes | None:
    """``path``'s content if it is a regular file (not a symlink, FIFO or device)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return file.read()


def _snapshot_git_dir(repo: Path, git_dir: Path, into: Path) -> bool:
    """Copy what ``git status`` needs from a worktree's git dir (HEAD and the index), so
    status reads nothing else the agent wrote there, its ``config.worktree`` included."""
    head = _read_regular_bytes(git_dir / "HEAD")
    if head is None:
        return False
    (into / "HEAD").write_bytes(head)
    # Git's ref store takes the common dir from this file, not from GIT_COMMON_DIR.
    (into / "commondir").write_text(f"{repo.absolute() / '.git'}\n")
    for name in ["index", *(p.name for p in git_dir.glob("sharedindex.*"))]:
        data = _read_regular_bytes(git_dir / name)
        if data is not None:
            (into / name).write_bytes(data)
    return True


class SubprocessGit:
    """Runs the real ``git`` binary, never trusting what the agent can write.

    Every call names its git dir and common dir explicitly (so a ``commondir`` or ``.git``
    file the agent planted can't point git at config of its choosing) and runs with
    isolated config (see ``_PINNED_CONFIG``).
    """

    async def _run(self, repo: Path, *args: str) -> GitResult:
        """Run git on the source repo ``repo``."""
        git_dir = repo / ".git"
        if not git_dir.is_dir():
            # Not a repository (or a linked worktree, which sandbox never uses as a source).
            return await self._exec(repo, {}, *args)
        if (git_dir / "commondir").exists():
            # Only a linked worktree's git dir has one; git would take refs from wherever it
            # points, so a source holding one was tampered with.
            return GitResult(128, "", f"refusing {git_dir}: it holds a commondir file")
        return await self._exec(
            repo, {"GIT_DIR": str(git_dir), "GIT_COMMON_DIR": str(git_dir)}, *args
        )

    async def _exec(self, cwd: Path, env: dict[str, str], *args: str) -> GitResult:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(cwd),
                *args,
                env=_isolated_environment(env),
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

    async def worktree_status(self, repo: Path, path: Path) -> WorktreeStatus | None:
        if not path.is_dir():
            return None
        git_dir = worktree_git_dir(repo, path)
        if git_dir is None:
            return None
        with tempfile.TemporaryDirectory(prefix="sandbox-status-") as snapshot:
            if not _snapshot_git_dir(repo, git_dir, Path(snapshot)):
                return None
            env = {
                "GIT_DIR": snapshot,
                "GIT_COMMON_DIR": str(repo / ".git"),
                "GIT_WORK_TREE": str(path),
            }
            result = await self._exec(
                path,
                env,
                "--no-optional-locks",
                "status",
                "--porcelain=v2",
                "--branch",
                "--ignore-submodules=all",
            )
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
        if not force:
            # ``git worktree remove`` would check for changes by running ``git status`` on
            # the worktree's own git dir, whose config.worktree the agent writes.
            status = await self.worktree_status(repo, path)
            if status is None:
                raise GitCommandError(f"git worktree remove: can't tell whether {path} is clean")
            if status.modified or status.untracked:
                raise GitCommandError(
                    f"git worktree remove: {path} contains modified or untracked files"
                )
        await self._check(repo, "worktree", "remove", "--force", str(path))

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
