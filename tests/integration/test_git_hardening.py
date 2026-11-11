"""Host-side git against the agent's clone: it syncs the agent's branch and reads its status,
and never runs code the agent wrote nor writes where the agent's symlinks point.

The agent owns everything in its clone (``<set dir>/<repo>``), ``.git`` included. These tests
plant what git would run or follow there directly, then call :class:`SubprocessGit` as
``up``, ``status``, ``down`` and ``destroy`` do. The agent's mounts (that it can't write the
source repo's ``.git`` at all) are checked in tests/integration/test_agent_git_isolation.py.
"""

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from sandbox.git import SubprocessGit, SyncOutcome
from tests.integration.conftest import git

pytestmark = pytest.mark.integration

BRANCH = "agent/jira-123"


def _clone(source: Path, tmp_path: Path) -> Path:
    path = tmp_path / "set" / "api"
    path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(SubprocessGit().clone_for_agent(source, path, BRANCH, base="main"))
    return path


def _commit(repo: Path, message: str) -> str:
    (repo / f"{message.replace(' ', '-')}.txt").write_text(message + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def _sync(source: Path, clone: Path) -> SyncOutcome:
    return asyncio.run(SubprocessGit().sync_agent_branch(source, clone, BRANCH)).outcome


def _touch(marker: Path) -> str:
    return f"touch {marker}"


def _plant_code(clone: Path, marker: Path) -> None:
    """Everything git could run from a repository's own config, hooks and attributes."""
    evil = _touch(marker)
    (clone / ".git" / "evil.cfg").write_text(
        f'[filter "pwn"]\n\tclean = tee {marker}\n\tsmudge = tee {marker}\n'
        f"\trequired = true\n[core]\n\tfsmonitor = {evil}\n"
    )
    for key, value in (
        ("include.path", str(clone / ".git" / "evil.cfg")),
        ("core.fsmonitor", evil),
        ("core.hooksPath", str(clone / ".git" / "hooks")),
        ("uploadpack.packObjectsHook", evil),
        ("core.alternateRefsCommand", evil),
        ("diff.external", evil),
    ):
        git(clone, "config", key, value)
    hooks = clone / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "reference-transaction", "post-index-change"):
        (hooks / name).write_text(f"#!/bin/sh\n{evil}\n")
        (hooks / name).chmod(0o755)
    (clone / ".gitattributes").write_text("* filter=pwn\n")


# --- syncing the agent's branch into the developer's repo ---------------------------------


def test_the_agents_clone_shares_the_sources_objects_and_starts_at_base(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    clone = _clone(source, tmp_path)

    alternates = (clone / ".git" / "objects" / "info" / "alternates").read_text()
    assert alternates == f"{source / '.git' / 'objects'}\n"
    assert git(clone, "branch", "--show-current") == BRANCH
    assert git(clone, "rev-parse", "HEAD") == git(source, "rev-parse", "main")
    # Not a worktree of the source: nothing of the clone lives in the source's .git.
    assert not (source / ".git" / "worktrees").exists()


def test_sync_brings_the_agents_commits_into_the_source_as_its_branch(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    clone = _clone(source, tmp_path)
    assert _sync(source, clone) is SyncOutcome.UPDATED  # the branch appears, at base
    assert git(source, "rev-parse", BRANCH) == git(source, "rev-parse", "main")

    first = _commit(clone, "agent work")
    assert git(source, "rev-parse", BRANCH) != first  # not live: only a sync moves it
    assert _sync(source, clone) is SyncOutcome.UPDATED
    assert git(source, "rev-parse", BRANCH) == first
    assert _sync(source, clone) is SyncOutcome.UNCHANGED

    second = _commit(clone, "more agent work")
    assert _sync(source, clone) is SyncOutcome.UPDATED
    assert git(source, "log", "--format=%s", BRANCH).splitlines()[:2] == [
        "more agent work",
        "agent work",
    ]
    assert git(source, "rev-parse", BRANCH) == second
    assert git(source, "fsck", "--no-progress") == ""
    assert git(source, "branch", "--show-current") == "main"


def test_sync_follows_history_the_agent_rewrote_when_the_developer_left_the_branch_alone(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    clone = _clone(source, tmp_path)
    _commit(clone, "agent work")
    _sync(source, clone)
    git(clone, "commit", "--quiet", "--amend", "-m", "agent work, amended")

    assert _sync(source, clone) is SyncOutcome.UPDATED
    assert git(source, "log", "-1", "--format=%s", BRANCH) == "agent work, amended"


def test_sync_never_overwrites_commits_the_developer_made_on_the_branch(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    clone = _clone(source, tmp_path)
    _sync(source, clone)
    developers = git(source, "commit-tree", "-p", BRANCH, "-m", "dev", f"{BRANCH}^{{tree}}")
    git(source, "update-ref", f"refs/heads/{BRANCH}", developers)
    agents = _commit(clone, "agent work")

    result = asyncio.run(SubprocessGit().sync_agent_branch(source, clone, BRANCH))

    assert result.outcome is SyncOutcome.DIVERGED
    assert f"refs/sandbox/{BRANCH}" in result.detail
    assert git(source, "rev-parse", BRANCH) == developers
    assert git(source, "rev-parse", f"refs/sandbox/{BRANCH}") == agents


def test_sync_leaves_the_branch_alone_while_the_developer_has_it_checked_out(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    clone = _clone(source, tmp_path)
    _sync(source, clone)
    git(source, "checkout", "--quiet", BRANCH)
    _commit(clone, "agent work")

    assert _sync(source, clone) is SyncOutcome.CHECKED_OUT
    assert git(source, "rev-parse", BRANCH) == git(source, "rev-parse", "main")
    assert git(source, "status", "--porcelain") == ""


def test_sync_of_a_missing_clone_does_nothing(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    assert _sync(source, tmp_path / "set" / "gone") is SyncOutcome.NO_CLONE


# --- what the agent plants in its clone reaches nothing on the host -----------------------


def test_sync_and_status_run_nothing_the_agent_planted_in_its_clone(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    clone = _clone(source, tmp_path)
    agents = _commit(clone, "agent work")
    marker = tmp_path / "pwned"
    _plant_code(clone, marker)
    # A tracked file whose content changed: status must hash it (a clean filter's moment).
    (clone / "agent-work.txt").write_text("changed by the agent\n")

    outcome = _sync(source, clone)
    status = asyncio.run(SubprocessGit().clone_status(source, clone))

    assert not marker.exists()
    assert outcome is SyncOutcome.UPDATED
    assert git(source, "rev-parse", BRANCH) == agents
    assert status is not None
    assert status.branch == BRANCH
    assert {".gitattributes", "agent-work.txt"} <= set(status.changes)
    assert "pwn" not in (source / ".git" / "config").read_text()


def test_host_git_writes_nothing_where_the_agents_symlinks_point(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    # The old route: host git appended to logs/ and wrote refs/ and objects/ through
    # symlinks the agent made in the shared .git. Now host git writes only in the source.
    source = make_repo("api")
    clone = _clone(source, tmp_path)
    agents = _commit(clone, "agent work")
    victim = tmp_path / "victim"
    victim.mkdir()
    git_dir = clone / ".git"
    for name in ("logs", "objects/pack", "refs/tags"):
        shutil.rmtree(git_dir / name)
        (git_dir / name).symlink_to(victim)
    (git_dir / "refs" / "heads" / "agent" / "evil").symlink_to(victim)
    for name in ("FETCH_HEAD", "ORIG_HEAD", "index.lock", "shallow"):
        (git_dir / name).symlink_to(victim / name)

    outcome = _sync(source, clone)
    status = asyncio.run(SubprocessGit().clone_status(source, clone))

    assert outcome is SyncOutcome.UPDATED
    assert git(source, "rev-parse", BRANCH) == agents
    assert status is not None
    assert list(victim.iterdir()) == []


def test_a_clone_whose_git_dir_points_elsewhere_is_neither_synced_nor_read(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    marker = tmp_path / "pwned"
    # A git dir of the agent's own choosing, with config that would run code.
    evil = tmp_path / "evil"
    subprocess.run(["git", "init", "--quiet", str(evil)], check=True)
    git(evil, "config", "core.fsmonitor", _touch(marker))
    for kind in ("gitfile", "symlink"):
        clone = _clone(source, tmp_path / kind)
        shutil.rmtree(clone / ".git")
        if kind == "gitfile":
            (clone / ".git").write_text(f"gitdir: {evil / '.git'}\n")
        else:
            (clone / ".git").symlink_to(evil / ".git")

        assert _sync(source, clone) is SyncOutcome.NO_CLONE
        assert asyncio.run(SubprocessGit().clone_status(source, clone)) is None
    assert not marker.exists()


@pytest.mark.parametrize("redirect", ["commondir", "alternates"])
def test_a_clone_redirected_to_another_repository_is_not_synced(
    make_repo: Callable[[str], Path], tmp_path: Path, redirect: str
) -> None:
    source = make_repo("api")
    other = make_repo("other")
    clone = _clone(source, tmp_path)
    _sync(source, clone)
    before = git(source, "rev-parse", BRANCH)
    if redirect == "commondir":
        (clone / ".git" / "commondir").write_text(f"{other / '.git'}\n")
    else:
        alternates = clone / ".git" / "objects" / "info" / "alternates"
        alternates.write_text(alternates.read_text() + f"{other / '.git' / 'objects'}\n")

    assert _sync(source, clone) is SyncOutcome.FAILED
    assert git(source, "rev-parse", BRANCH) == before


def test_cloning_runs_no_hook_of_the_source_or_from_a_hooks_path_in_the_checkout(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    # e.g. husky: core.hooksPath=.husky resolves inside the checkout.
    source = make_repo("api")
    marker = tmp_path / "pwned"
    for hooks in (source / ".git" / "hooks", source / ".husky"):
        hooks.mkdir(exist_ok=True)
        (hooks / "post-checkout").write_text(f"#!/bin/sh\n{_touch(marker)}\n")
        (hooks / "post-checkout").chmod(0o755)
    git(source, "add", "-A")
    git(source, "commit", "--quiet", "-m", "hooks")
    git(source, "config", "core.hooksPath", ".husky")

    clone = _clone(source, tmp_path)

    assert (clone / ".husky" / "post-checkout").is_file()
    assert not marker.exists()


def test_a_commondir_planted_in_a_source_runs_nothing_and_is_refused(
    make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    source = make_repo("api")
    marker = tmp_path / "pwned"
    evil = tmp_path / "evil"
    subprocess.run(["git", "init", "--quiet", "--bare", str(evil)], check=True)
    git(evil, "config", "core.fsmonitor", _touch(marker))
    (source / ".git" / "commondir").write_text(f"{evil}\n")

    assert not asyncio.run(SubprocessGit().is_repository(source))
    assert not marker.exists()
