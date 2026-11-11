"""`up` against real git: worktrees, branch reuse, and concurrent processes."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from sandbox.compose import project_name
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git, unique_set

pytestmark = pytest.mark.integration


def test_up_creates_real_worktrees_on_agent_branch(
    env: Env, make_repo: Callable[[str], Path]
) -> None:
    api = make_repo("api")
    web = make_repo("web")
    git(web, "checkout", "--quiet", "-b", "release/2.4")

    result = invoke(["jira-123", "up", "--repos", "api", "web"], SubprocessGit())

    assert result.exit_code == 0, result.output
    set_dir = env.set_dir("jira-123")
    plan = yaml.safe_load((set_dir / "sandbox.yaml").read_text())
    assert plan["repos"]["web"] == {"source": str(web.resolve()), "base": "release/2.4"}
    for repo in (api, web):
        worktree = set_dir / repo.name
        assert git(worktree, "branch", "--show-current") == "agent/jira-123"
        assert str(worktree) in git(repo, "worktree", "list")
    # The source checkouts are untouched.
    assert git(api, "branch", "--show-current") == "main"
    assert git(web, "branch", "--show-current") == "release/2.4"


def test_non_repo_dir_fails_before_anything_is_created(
    env: Env, make_repo: Callable[[str], Path]
) -> None:
    make_repo("api")
    env.make_repo_dir("plain")

    result = invoke(["jira-123", "up", "--repos", "api", "plain"], SubprocessGit())

    assert result.exit_code == 1
    assert "plain:" in result.stderr
    assert not env.sets_root.exists()


def test_later_up_reuses_surviving_branch_and_its_commits(
    env: Env, make_repo: Callable[[str], Path]
) -> None:
    api = make_repo("api")
    assert invoke(["jira-123", "up", "--repos", "api"], SubprocessGit()).exit_code == 0
    worktree = env.set_dir("jira-123") / "api"
    (worktree / "work.txt").write_text("agent work\n")
    git(worktree, "add", "work.txt")
    git(worktree, "commit", "--quiet", "-m", "agent work")
    # Simulate the set directory being lost while the branch survives.
    git(api, "worktree", "remove", "--force", str(worktree))
    (env.set_dir("jira-123") / "sandbox.yaml").unlink()
    git(api, "commit", "--quiet", "--allow-empty", "-m", "main moved on")

    result = invoke(["jira-123", "up", "--repos", "api"], SubprocessGit())

    assert result.exit_code == 0, result.output
    assert "base 'main' from the plan was ignored" in result.stderr
    assert (worktree / "work.txt").read_text() == "agent work\n"


def test_concurrent_up_processes_on_the_same_repo(
    env: Env, make_repo: Callable[[str], Path]
) -> None:
    api = make_repo("api")
    web = make_repo("web")
    count = 8
    sets = [unique_set(f"set-{index}") for index in range(count)]
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "sandbox", set_name, "up", "--repos", "api", "web"],
            cwd=env.cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for set_name in sets
    ]
    try:
        outputs = [process.communicate(timeout=240) for process in processes]
    finally:
        # Each set also started its agent container.
        for set_name in sets:
            subprocess.run(
                ["docker", "compose", "-p", project_name(set_name), "down", "--volumes"],
                capture_output=True,
                check=False,
            )

    for process, (stdout, stderr) in zip(processes, outputs, strict=True):
        assert process.returncode == 0, stdout + stderr
    for repo in (api, web):
        worktrees = git(repo, "worktree", "list", "--porcelain")
        for set_name in sets:
            assert f"branch refs/heads/agent/{set_name}" in worktrees
        assert git(repo, "fsck", "--no-progress") == ""
