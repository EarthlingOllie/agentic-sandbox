"""The git operations sandbox needs, behind a small protocol so unit tests can use a fake.

The agent never works in the developer's repo. Each repo of a set is the agent's own clone
in the set directory (``<set dir>/<repo>``), made by host git with ``--shared``: it borrows
the source repo's objects through ``objects/info/alternates``, and the agent sees the
source's ``.git`` only read-only. Everything in the clone (config, hooks, refs, objects,
symlinks) is the agent's, so host git treats it as an untrusted repository:

* :meth:`SubprocessGit.sync_agent_branch` brings the agent's ``agent/<set>`` into the
  source repo with ``git fetch`` from the clone. Host git writes only in the source; on the
  clone's side it runs ``git upload-pack --strict``, which git documents as safe to run in
  an untrusted repository (it runs no hooks and none of the repository's commands).
* :meth:`SubprocessGit.clone_status` reads the clone's ``HEAD``, branch and index without
  following symlinks, into a throwaway git dir sandbox writes itself, and runs ``git
  status`` there: the clone's config and hooks are never read.
"""

import asyncio
import os
import re
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from sandbox.errors import SandboxError


@dataclass(frozen=True)
class CloneStatus:
    branch: str | None  # None on a detached HEAD
    modified: bool  # staged or unstaged changes to tracked files (or conflicts)
    untracked: bool  # files git doesn't track and doesn't ignore
    # The paths behind ``modified`` and ``untracked``, as ``git status`` names them.
    changes: tuple[str, ...] = ()


class SyncOutcome(StrEnum):
    UPDATED = "updated"  # the source's branch now holds the agent's commits
    UNCHANGED = "unchanged"  # it already did
    NO_CLONE = "no-clone"  # the set has no clone of this repo (nothing to sync)
    # Not synced: the source's branch is checked out, so moving it would change files there.
    CHECKED_OUT = "checked-out"
    # Not synced: the source's branch has commits the agent's branch doesn't hold.
    DIVERGED = "diverged"
    FAILED = "failed"  # e.g. the clone has no such branch or was tampered with


# Outcomes after which the source's branch holds the agent's work.
SYNCED: Final = frozenset({SyncOutcome.UPDATED, SyncOutcome.UNCHANGED, SyncOutcome.NO_CLONE})


@dataclass(frozen=True)
class SyncResult:
    outcome: SyncOutcome
    detail: str = ""


def agent_tip_ref(branch: str) -> str:
    """Where the source repo keeps the agent's latest tip of ``branch``, whatever became of
    the branch itself: hidden from ``git branch`` and protecting the agent's objects."""
    return f"refs/sandbox/{branch}"


class Git(Protocol):
    async def is_repository(self, path: Path) -> bool: ...

    async def current_branch(self, repo: Path) -> str | None:
        """The checked-out branch, or None on a detached HEAD."""
        ...

    async def branch_exists(self, repo: Path, branch: str) -> bool: ...

    async def revision_exists(self, repo: Path, revision: str) -> bool: ...

    async def clone_for_agent(
        self, repo: Path, path: Path, branch: str, *, base: str | None
    ) -> None:
        """Clone ``repo`` at ``path``, sharing its objects, checked out on a new ``branch``.

        With ``base``, the branch starts there; without, at ``repo``'s own ``branch``.
        """
        ...

    async def clone_status(self, repo: Path, path: Path) -> CloneStatus | None:
        """The branch of the agent's clone at ``path`` and whether it has uncommitted or
        untracked changes, or None when ``path`` is no clone sandbox can read (e.g. it was
        deleted)."""
        ...

    async def sync_agent_branch(self, repo: Path, path: Path, branch: str) -> SyncResult:
        """Bring ``branch`` of the agent's clone at ``path`` into ``repo``'s ``branch``."""
        ...

    async def forget_agent_tip(self, repo: Path, branch: str) -> None:
        """Drop ``repo``'s record of the agent's tip of ``branch`` (see :func:`agent_tip_ref`)."""
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


# Host git never runs code the agent chose. The agent can't write the source repo's ``.git``
# at all and host git never reads the clone's config (see the module docstring); on top of
# that every call runs with isolated config: no system or global config, nothing from the
# environment, and every setting that names a program to run pinned off.
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
    ("fetch.recurseSubmodules", "false"),
    ("status.submoduleSummary", "false"),
    ("credential.helper", ""),
    ("protocol.allow", "never"),
    ("log.showSignature", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)

# Git's environment variables name config, a repository or a program; only identity is kept.
_KEPT_GIT_ENV = ("GIT_AUTHOR_", "GIT_COMMITTER_")
# Clone and fetch from the agent's clone, a local path, need the file transport.
_ALLOW_LOCAL = ("-c", "protocol.file.allow=always")

_HEX_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BRANCH_REF = re.compile(r"^refs/heads/(?!.*(?:\.\.|//|/\.|@\{|\.lock(?:/|$)))[A-Za-z0-9._/-]+$")
# More than any HEAD, ref or packed-refs file of a clone sandbox made holds.
_REF_LIMIT = 64 << 20
_INDEX_LIMIT = 1 << 34
# The minimal config of the throwaway git dir status runs in.
_SNAPSHOT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"


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


# --- reading what the agent wrote, never following its symlinks ---------------------------


def _read_beneath(root: Path, relative: str, limit: int = _REF_LIMIT) -> bytes | None:
    """The content of regular file ``root/relative``, or None. No symlink is followed: not
    ``root`` itself, not any directory on the way, not the file (nor a FIFO or device)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        directory = os.open(root, flags | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        *parents, leaf = relative.split("/")
        for part in parents:
            inner = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = inner
        fd = os.open(leaf, flags | os.O_NONBLOCK, dir_fd=directory)
    except OSError:
        return None
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= limit and (chunk := file.read(1 << 20)):
            chunks.append(chunk)
            size += len(chunk)
    return b"".join(chunks) if size <= limit else None


def _is_plain_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _clone_git_dir(path: Path) -> Path | None:
    """The clone's ``.git`` directory, or None unless both it and ``path`` are real
    directories (not symlinks, not a ``.git`` file pointing elsewhere)."""
    git_dir = path / ".git"
    return git_dir if _is_plain_dir(path) and _is_plain_dir(git_dir) else None


@dataclass(frozen=True)
class _Head:
    ref: str | None  # ``refs/heads/<branch>``, or None on a detached HEAD
    commit: str | None  # None on an unborn branch


def _read_head(git_dir: Path) -> _Head | None:
    """The clone's HEAD and the commit it names, read without git; None if unreadable."""
    raw = _read_beneath(git_dir, "HEAD", 4096)
    if raw is None:
        return None
    text = raw.decode("ascii", errors="replace").strip()
    if _HEX_ID.match(text):
        return _Head(None, text)
    ref = text.removeprefix("ref: ")
    if ref == text or not _BRANCH_REF.match(ref):
        return None
    return _Head(ref, _read_ref(git_dir, ref))


def _read_ref(git_dir: Path, ref: str) -> str | None:
    loose = _read_beneath(git_dir, ref, 4096)
    if loose is not None:
        value = loose.decode("ascii", errors="replace").strip()
        return value if _HEX_ID.match(value) else None
    packed = _read_beneath(git_dir, "packed-refs")
    for line in (packed or b"").decode("utf-8", errors="replace").splitlines():
        value, _, name = line.partition(" ")
        if name == ref and _HEX_ID.match(value):
            return value
    return None


def _snapshot(repo: Path, git_dir: Path, head: _Head, into: Path) -> None:
    """A git dir of sandbox's own for ``git status`` on the clone: its HEAD, branch and
    index, sandbox's config, and the clone's and the source's objects (read, never written:
    status runs with ``--no-optional-locks``)."""
    (into / "config").write_text(_SNAPSHOT_CONFIG)
    (into / "objects" / "info").mkdir(parents=True)
    alternates = f"{git_dir.absolute() / 'objects'}\n{repo.absolute() / '.git' / 'objects'}\n"
    (into / "objects" / "info" / "alternates").write_text(alternates)
    (into / "refs").mkdir()
    if head.ref is None:
        (into / "HEAD").write_text(f"{head.commit}\n")
    else:
        (into / "HEAD").write_text(f"ref: {head.ref}\n")
        if head.commit is not None:
            ref_file = into / head.ref
            ref_file.parent.mkdir(parents=True, exist_ok=True)
            ref_file.write_text(f"{head.commit}\n")
    index = _read_beneath(git_dir, "index", _INDEX_LIMIT)
    if index is not None:
        (into / "index").write_bytes(index)
    # A split index keeps most entries in a shared file next to it.
    for name in (path.name for path in git_dir.iterdir()):
        if name.startswith("sharedindex."):
            data = _read_beneath(git_dir, name, _INDEX_LIMIT)
            if data is not None:
                (into / name).write_bytes(data)


def _parse_status(output: str) -> CloneStatus:
    branch: str | None = None
    modified = untracked = False
    changes: list[str] = []
    for line in output.splitlines():
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
    return CloneStatus(branch, modified, untracked, tuple(changes))


class SubprocessGit:
    """Runs the real ``git`` binary, never trusting what the agent can write.

    Every call on a source repo names its git dir explicitly, and every call runs with
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

    @staticmethod
    def _checked(result: GitResult, where: Path, args: tuple[str, ...]) -> str:
        if result.returncode != 0:
            raise GitCommandError(
                f"git {' '.join(args)} failed in {where}: {result.stderr or result.stdout}"
            )
        return result.stdout

    async def _check(self, repo: Path, *args: str) -> str:
        return self._checked(await self._run(repo, *args), repo, args)

    async def _commit(self, repo: Path, ref: str) -> str | None:
        result = await self._run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return result.stdout if result.returncode == 0 and result.stdout else None

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
        return await self._commit(repo, revision) is not None

    async def clone_for_agent(
        self, repo: Path, path: Path, branch: str, *, base: str | None
    ) -> None:
        start = base if base is not None else f"refs/heads/{branch}"
        commit = await self._commit(repo, start)
        if commit is None:
            raise GitCommandError(f"{start!r} is not a commit in {repo}")
        # Built next to its final place and renamed there once checked out, so a clone
        # interrupted half-way is never taken for a finished one.
        staging = path.with_name(f".{path.name}.clone-{secrets.token_hex(4)}")
        try:
            args = (
                *_ALLOW_LOCAL, "clone", "--shared", "--no-checkout", "--quiet", "--template=",
                "--", str(repo.absolute()), str(staging),
            )  # fmt: skip
            self._checked(await self._exec(path.parent, {}, *args), path.parent, args)
            env = {"GIT_DIR": str(staging / ".git"), "GIT_WORK_TREE": str(staging)}
            args = ("checkout", "--quiet", "-b", branch, commit)
            self._checked(await self._exec(staging, env, *args), staging, args)
            staging.rename(path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def clone_status(self, repo: Path, path: Path) -> CloneStatus | None:
        git_dir = _clone_git_dir(path)
        if git_dir is None:
            return None
        head = _read_head(git_dir)
        if head is None:
            return None
        with tempfile.TemporaryDirectory(prefix="sandbox-status-") as snapshot:
            _snapshot(repo, git_dir, head, Path(snapshot))
            env = {"GIT_DIR": snapshot, "GIT_WORK_TREE": str(path)}
            result = await self._exec(
                path,
                env,
                "--no-optional-locks",
                "status",
                "--porcelain=v2",
                "--branch",
                "--ignore-submodules=all",
            )
        return _parse_status(result.stdout) if result.returncode == 0 else None

    async def sync_agent_branch(self, repo: Path, path: Path, branch: str) -> SyncResult:
        git_dir = _clone_git_dir(path)
        if git_dir is None:
            return SyncResult(SyncOutcome.NO_CLONE)
        expected = f"{(repo / '.git' / 'objects').absolute()}\n".encode()
        alternates = _read_beneath(git_dir, "objects/info/alternates", 1 << 16)
        if os.path.lexists(git_dir / "commondir") or alternates not in (None, expected):
            # Either would have git read refs or objects from elsewhere on this machine.
            return SyncResult(SyncOutcome.FAILED, f"{git_dir} was changed to point elsewhere")
        tip_ref = agent_tip_ref(branch)
        previous = await self._commit(repo, tip_ref)
        fetched = await self._run(
            repo,
            *_ALLOW_LOCAL,
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            "--no-tags",
            "--no-recurse-submodules",
            # --strict: serve exactly this git dir, never a ``.git`` the agent put in it.
            "--upload-pack=git-upload-pack --strict",
            str(git_dir.absolute()),
            f"+refs/heads/{branch}:{tip_ref}",
        )
        if fetched.returncode != 0:
            return SyncResult(SyncOutcome.FAILED, fetched.stderr or fetched.stdout)
        tip = await self._commit(repo, tip_ref)
        current = await self._commit(repo, f"refs/heads/{branch}")
        if tip is None:
            return SyncResult(SyncOutcome.FAILED, f"the clone's {branch} is not a commit")
        if current == tip:
            return SyncResult(SyncOutcome.UNCHANGED)
        if await self._checked_out(repo, branch):
            return SyncResult(
                SyncOutcome.CHECKED_OUT,
                f"{branch} is checked out in {repo}; the agent's commits are at {tip_ref}",
            )
        if current is not None and current != previous:
            merged = await self._run(repo, "merge-base", "--is-ancestor", current, tip)
            if merged.returncode != 0:
                return SyncResult(
                    SyncOutcome.DIVERGED,
                    f"{branch} in {repo} has commits the agent's branch doesn't; "
                    f"the agent's commits are at {tip_ref}",
                )
        # ``current`` as the expected old value: a branch the developer moved meanwhile
        # makes this fail rather than be overwritten.
        message = f"sandbox: sync from {path}"
        updated = await self._run(
            repo, "update-ref", "-m", message, f"refs/heads/{branch}", tip, current or ""
        )
        if updated.returncode != 0:
            return SyncResult(SyncOutcome.FAILED, updated.stderr)
        return SyncResult(SyncOutcome.UPDATED)

    async def _checked_out(self, repo: Path, branch: str) -> bool:
        listing = await self._check(repo, "worktree", "list", "--porcelain")
        return f"branch refs/heads/{branch}" in listing.splitlines()

    async def forget_agent_tip(self, repo: Path, branch: str) -> None:
        if await self._commit(repo, agent_tip_ref(branch)) is not None:
            await self._check(repo, "update-ref", "-d", agent_tip_ref(branch))

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
