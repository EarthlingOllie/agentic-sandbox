"""The agent can use git in its worktree but can't get host-side git to run its code.

Against real Docker and git: the agent commits, branches and fetches; then the host rewrites
the source repo's ``.git/config`` by renaming a new file over it (as ``git config`` and
``git worktree add`` do), and the agent plants everything git would run (a filter, an
include, hooks, fsmonitor, a per-worktree config). Host-side git, through sandbox's
``status`` and ``destroy`` and :class:`SubprocessGit`, must run none of it.

The agent's commands run as the owner of the host files (the agent user, unless the tests
run as root), so what stops a write is the mount layout, not file permissions.
"""

import asyncio
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

SET = unique_set("gitiso")
PROJECT = project_name(SET)
BRANCH = f"agent/{SET}"
IDENTITY = "-c user.name=agent -c user.email=agent@example.invalid"


def as_agent(script: str, *, workdir: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in the agent container as the owner of the host files."""
    return subprocess.run(
        [
            "docker", "compose", "-p", PROJECT, "exec", "-T",
            "-u", f"{os.getuid()}:{os.getgid()}", "-w", workdir,
            "agent", "sh", "-c", script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip


def ok(script: str, *, workdir: str) -> str:
    result = as_agent(script, workdir=workdir)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture
def api(env: Env, make_repo: Callable[[str], Path]) -> Iterator[Path]:
    path = make_repo("api")
    result = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())
    assert result.exit_code == 0, result.output
    yield path
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


def _rewrite_config_on_host(source: Path, extra: str) -> None:
    """Replace ``.git/config`` the way git does: write a new file, rename it over the old."""
    config = source / ".git" / "config"
    replacement = config.with_name("config.new")
    replacement.write_text(config.read_text() + extra)
    replacement.replace(config)


def test_agent_commits_branches_and_fetches_in_its_worktree(env: Env, api: Path) -> None:
    worktree = f"/workspace/{SET}/api"
    ok(
        f"echo work > work.txt && git add work.txt && git {IDENTITY} commit -q -m 'agent work'"
        " && git branch agent-side"
        f" && git fetch -q {api}/.git main:refs/remotes/origin/main"
        " && git rev-parse -q --verify FETCH_HEAD",
        workdir=worktree,
    )

    assert git(api, "log", "-1", "--format=%s", BRANCH) == "agent work"
    assert git(api, "rev-parse", "agent-side") == git(api, "rev-parse", BRANCH)
    assert git(api, "rev-parse", "refs/remotes/origin/main") == git(api, "rev-parse", "main")


def test_agent_cannot_get_host_git_to_run_code_after_the_host_rewrites_config(
    env: Env, api: Path, tmp_path: Path
) -> None:
    worktree = f"/workspace/{SET}/api"
    git_dir = api / ".git"
    marker = tmp_path / "pwned"
    evil = f"touch {marker}"
    # A per-worktree config is read once the repo turns it on (git sparse-checkout does);
    # the host turning it on is one more rewrite of .git/config by rename.
    _rewrite_config_on_host(api, "[extensions]\n\tworktreeConfig = true\n")
    git(api, "config", "sandbox.touched", "yes")  # git's own rename over .git/config

    # tee passes the content through, so git sees a working filter.
    filter_config = f'[filter "pwn"]\n\tclean = tee {marker}\n\tsmudge = tee {marker}\n'
    attempts = {
        "config": f"printf '%s' '{filter_config}[core]\n\tfsmonitor = {evil}\n"
        f"\thooksPath = {git_dir}/hooks\n' >> {git_dir}/config",
        "replace config": f"cp {git_dir}/config /tmp/c && printf '%s' '{filter_config}' >> /tmp/c"
        f" && mv /tmp/c {git_dir}/config",
        "include": f"printf '%s' '{filter_config}' > {git_dir}/evil.cfg"
        f" && git config --file {git_dir}/config include.path {git_dir}/evil.cfg",
        "hook": f"printf '#!/bin/sh\\n{evil}\\n' > {git_dir}/hooks/post-checkout"
        f" && chmod +x {git_dir}/hooks/post-checkout",
        "worktree config": f"printf '%s' '{filter_config}'"
        f" > {git_dir}/worktrees/api/config.worktree",
        "attributes": "printf '* filter=pwn\\n' > .gitattributes && touch .sandbox/compose.yaml",
    }
    for attempt in attempts.values():
        as_agent(attempt, workdir=worktree)

    resumed = invoke([SET, "up"], SubprocessGit(), SubprocessCompose())
    status = asyncio.run(SubprocessGit().worktree_status(api, env.set_dir(SET) / "api"))
    listed = invoke([SET, "status"], SubprocessGit())
    stopped = invoke([SET, "down"], SubprocessGit(), SubprocessCompose())
    destroyed = invoke(
        [SET, "destroy", "--force", "--branches"], SubprocessGit(), SubprocessCompose()
    )

    assert not marker.exists()
    assert status is not None
    assert status.branch == BRANCH
    assert ".gitattributes" in status.changes
    assert resumed.exit_code == 0, resumed.output
    assert listed.exit_code == 0, listed.output
    assert stopped.exit_code == 0, stopped.output
    assert destroyed.exit_code == 0, destroyed.output
    assert "pwn" not in (git_dir / "config").read_text()
    assert not (git_dir / "hooks" / "post-checkout").exists()
