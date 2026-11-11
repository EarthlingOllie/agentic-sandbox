"""Host-side git never runs code the agent wrote.

The agent mounts each source repo's ``.git`` read-write (and the set directory, which holds
each worktree's ``.git`` file), so anything git reads from there is the agent's to choose.
These tests plant what an agent could and check that :class:`SubprocessGit` runs none of
it; the agent mount of ``.git/config`` being read-only is checked in tests/unit/test_agent.py.
"""

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from sandbox.git import GitCommandError, SubprocessGit
from tests.integration.conftest import git

pytestmark = pytest.mark.integration


def _worktree(source: Path, path: Path) -> None:
    git(source, "worktree", "add", "--quiet", "-b", "agent/jira-123", str(path), "main")


def _touch(marker: Path) -> str:
    return f"touch {marker}"


def test_status_of_a_worktree_ignores_an_fsmonitor_the_agent_set(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    worktree = tmp_path / "set" / "api"
    _worktree(source, worktree)
    marker = tmp_path / "pwned"
    git(source, "config", "core.fsmonitor", _touch(marker))

    status = asyncio.run(SubprocessGit().worktree_status(source, worktree))

    assert status is not None
    assert status.branch == "agent/jira-123"
    assert not marker.exists()


def test_add_worktree_runs_no_hook_the_agent_planted(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    marker = tmp_path / "pwned"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\n{_touch(marker)}\n")
    hook.chmod(0o755)

    asyncio.run(
        SubprocessGit().add_worktree(source, tmp_path / "set" / "api", "agent/x", base="main")
    )

    assert (tmp_path / "set" / "api" / ".git").is_file()
    assert not marker.exists()


def test_add_worktree_runs_no_hook_from_a_hooks_path_inside_the_worktree(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    # e.g. husky: core.hooksPath=.husky resolves inside each worktree, which the agent writes.
    source = make_repo("api")
    marker = tmp_path / "pwned"
    hooks = source / ".husky"
    hooks.mkdir()
    (hooks / "post-checkout").write_text(f"#!/bin/sh\n{_touch(marker)}\n")
    (hooks / "post-checkout").chmod(0o755)
    git(source, "add", "-A")
    git(source, "commit", "--quiet", "-m", "hooks")
    git(source, "config", "core.hooksPath", ".husky")

    asyncio.run(
        SubprocessGit().add_worktree(source, tmp_path / "set" / "api", "agent/x", base="main")
    )

    assert not marker.exists()


def test_status_does_not_follow_a_worktree_git_file_the_agent_replaced(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    worktree = tmp_path / "set" / "api"
    _worktree(source, worktree)
    marker = tmp_path / "pwned"
    # A git dir of the agent's own, inside the set directory, with config it chose.
    evil = tmp_path / "set" / "evil"
    subprocess.run(["git", "init", "--quiet", "--bare", str(evil)], check=True)
    git(evil, "config", "core.fsmonitor", _touch(marker))
    git(evil, "config", "core.bare", "false")
    (worktree / ".git").write_text(f"gitdir: {evil}\n")

    status = asyncio.run(SubprocessGit().worktree_status(source, worktree))

    assert status is None
    assert not marker.exists()


def test_a_commondir_the_agent_planted_runs_nothing_and_is_refused(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    worktree = tmp_path / "set" / "api"
    _worktree(source, worktree)
    marker = tmp_path / "pwned"
    evil = tmp_path / "evil"
    subprocess.run(["git", "init", "--quiet", "--bare", str(evil)], check=True)
    git(evil, "config", "core.fsmonitor", _touch(marker))
    # Points both the source and the worktree's own git dir at the agent's config.
    (source / ".git" / "commondir").write_text(f"{evil}\n")
    (source / ".git" / "worktrees" / "api" / "commondir").write_text(f"{evil}\n")
    git_ = SubprocessGit()

    async def scenario() -> None:
        assert not await git_.is_repository(source)
        with pytest.raises(GitCommandError, match="commondir"):
            await git_.prune_worktrees(source)
        await git_.worktree_status(source, worktree)

    asyncio.run(scenario())

    assert not marker.exists()
