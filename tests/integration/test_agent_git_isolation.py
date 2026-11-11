"""The agent works in its own clone and can't reach the host through git.

Against real Docker and git:

- the agent commits, branches and fetches in its clone, and ``status`` and ``down`` bring
  its commits into the developer's repo as ``agent/<set>`` (not live: only then);
- the agent can write nothing in the source repo's ``.git``, so neither the old routes
  (config rewritten after the host renames a new ``.git/config`` over the bind, symlinks
  under ``logs/``, ``refs/`` and ``objects/`` that host git then writes through) nor any
  other reach the host: not through sandbox's own git, not through the developer's;
- what the agent plants in its own clone (config, hooks, attributes, symlinks) runs nothing
  and receives no write when ``up``, ``status``, ``down`` and ``destroy`` run.

The agent's commands run as the owner of the host files (the agent user, unless the tests
run as root), so what stops a write is the mount layout, not file permissions.
"""

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
CLONE = f"/workspace/{SET}/api"
IDENTITY = "-c user.name=agent -c user.email=agent@example.invalid"


def as_agent(script: str, *, workdir: str = CLONE) -> subprocess.CompletedProcess[str]:
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


def ok(script: str, *, workdir: str = CLONE) -> str:
    result = as_agent(script, workdir=workdir)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def sandbox(*args: str) -> str:
    result = invoke([SET, *args], SubprocessGit(), SubprocessCompose())
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def api(env: Env, make_repo: Callable[[str], Path]) -> Iterator[Path]:
    path = make_repo("api")
    sandbox("up", "--repos", "api")
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


def test_agent_commits_appear_as_its_branch_in_the_repo_after_status_and_down(
    env: Env, api: Path
) -> None:
    base = git(api, "rev-parse", "main")
    assert git(api, "rev-parse", BRANCH) == base  # `up` synced the new branch

    ok(
        f"echo work > work.txt && git add work.txt && git {IDENTITY} commit -q -m 'agent work'"
        " && git branch agent-side && git fetch -q origin && git rev-parse -q --verify origin/main"
    )
    # Not live: the developer's repo moves only when sandbox syncs it.
    assert git(api, "rev-parse", BRANCH) == base

    sandbox("status")
    assert git(api, "log", "-1", "--format=%s", BRANCH) == "agent work"

    ok(f"echo more > more.txt && git add more.txt && git {IDENTITY} commit -q -m 'more work'")
    sandbox("down")
    assert git(api, "log", "--format=%s", BRANCH).splitlines()[:2] == ["more work", "agent work"]

    # Only agent/<set> comes back; the agent's other branches stay in its clone.
    assert "agent-side" not in git(api, "branch", "--list")
    assert git(api, "branch", "--show-current") == "main"
    assert git(api, "status", "--porcelain") == ""
    assert git(api, "fsck", "--no-progress") == ""


def test_agent_can_write_nothing_in_the_source_repos_git_dir(
    env: Env, api: Path, tmp_path: Path
) -> None:
    git_dir = api / ".git"
    marker = tmp_path / "pwned"
    victim = tmp_path / "victim"
    victim.mkdir()
    evil = f"touch {marker}"
    # Host git replacing .git/config by rename detached a read-only bind of the file alone.
    _rewrite_config_on_host(api, "[extensions]\n\tworktreeConfig = true\n")
    git(api, "config", "sandbox.touched", "yes")

    filter_config = f'[filter "pwn"]\n\tclean = tee {marker}\n\tsmudge = tee {marker}\n'
    attempts = {
        "append to config": f"printf '%s' '{filter_config}[core]\n\tfsmonitor = {evil}\n'"
        f" >> {git_dir}/config",
        "replace config": f"cp {git_dir}/config /tmp/c && printf '%s' '{filter_config}'"
        f" >> /tmp/c && mv /tmp/c {git_dir}/config",
        "include": f"printf '%s' '{filter_config}' > {git_dir}/evil.cfg",
        "hook": f"printf '#!/bin/sh\\n{evil}\\n' > {git_dir}/hooks/post-commit"
        f" && chmod +x {git_dir}/hooks/post-commit",
        # Host git then appended reflog lines (agent-chosen commit subjects) to the target.
        "symlinked logs": f"rm -rf {git_dir}/logs && ln -s {victim} {git_dir}/logs",
        "symlinked reflog": f"mkdir -p {git_dir}/logs/refs/heads"
        f" && ln -sf {victim}/main {git_dir}/logs/refs/heads/main",
        # ... wrote a ref file into the target directory ...
        "symlinked refs": f"ln -s {victim} {git_dir}/refs/heads/evil",
        # ... and loose objects or packs, whose bytes the agent can choose.
        "symlinked objects": f"rm -rf {git_dir}/objects/pack"
        f" && ln -s {victim} {git_dir}/objects/pack",
        "symlinked fan-out": f"ln -s {victim} {git_dir}/objects/ff",
        "write an object": f"echo x > {git_dir}/objects/written",
        "new ref": f"git --git-dir={git_dir} branch from-agent main",
        "packed-refs": f"echo > {git_dir}/packed-refs",
    }
    results = {name: as_agent(attempt) for name, attempt in attempts.items()}

    # The developer's own git in the repo, and sandbox's, after the agent's attempts.
    (api / "dev.txt").write_text("dev\n")
    git(api, "add", "dev.txt")
    git(api, "commit", "--quiet", "-m", "developer work")
    git(api, "branch", "evil/x")
    git(api, "gc", "--quiet")
    sandbox("up")
    sandbox("status")
    sandbox("down")
    sandbox("destroy", "--force", "--branches")

    assert {name for name, result in results.items() if result.returncode == 0} == set()
    assert not marker.exists()
    assert list(victim.iterdir()) == []
    assert "pwn" not in (git_dir / "config").read_text()
    assert not (git_dir / "hooks" / "post-commit").exists()
    assert not (git_dir / "logs").is_symlink()
    assert git(api, "fsck", "--no-progress") == ""


def test_nothing_the_agent_plants_in_its_clone_runs_or_is_written_on_the_host(
    env: Env, api: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "pwned"
    victim = tmp_path / "victim"
    victim.mkdir()
    evil = f"touch {marker}"
    clone_git = env.set_dir(SET) / "api" / ".git"
    ok(f"echo work > work.txt && git add work.txt && git {IDENTITY} commit -q -m 'agent work'")
    filter_config = f'[filter "pwn"]\n\tclean = tee {marker}\n\tsmudge = tee {marker}\n'
    ok(
        f"printf '%s' '{filter_config}' > .git/evil.cfg"
        " && git config include.path evil.cfg"
        f" && git config core.fsmonitor '{evil}'"
        f" && git config uploadpack.packObjectsHook '{evil}'"
        f" && git config core.hooksPath {clone_git}/hooks && mkdir -p .git/hooks"
        f" && printf '#!/bin/sh\\n{evil}\\n' > .git/hooks/reference-transaction"
        " && chmod +x .git/hooks/reference-transaction"
        " && printf '* filter=pwn\\n' > .gitattributes && echo changed > work.txt"
        f" && rm -rf .git/logs && ln -s {victim} .git/logs"
        f" && ln -s {victim} .git/refs/heads/agent/evil"
        f" && rm -rf .git/objects/pack && ln -s {victim} .git/objects/pack"
        f" && ln -s {victim}/FETCH_HEAD .git/FETCH_HEAD"
    )

    sandbox("up")
    status = sandbox("status")
    sandbox("down")
    synced = git(api, "log", "-1", "--format=%s", BRANCH)
    sandbox("destroy", "--force")

    assert not marker.exists()
    assert list(victim.iterdir()) == []
    assert "* uncommitted changes, ? untracked files" in status
    assert synced == "agent work"
