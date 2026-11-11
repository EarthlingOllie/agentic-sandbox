"""The agent container against real Docker: hardened, mounts, host UID, `git` in a worktree.

The first run builds the default agent image (Compose builds it on `up`), which pulls
`ubuntu:24.04` and installs `claude` from npm.
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

SET = unique_set("agent")
PROJECT = project_name(SET)
CONTAINER = f"{PROJECT}-agent-1"
RUNNING_AS_ROOT = os.getuid() == 0


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def in_agent(script: str, *, workdir: str | None = None) -> str:
    """Run ``script`` in the agent the way `sandbox sh` does (`compose exec`)."""
    where = ["-w", workdir] if workdir else []
    return docker("compose", "-p", PROJECT, "exec", "-T", *where, "agent", "sh", "-c", script)


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


def test_agent_is_hardened_and_ships_the_tools(api: Path) -> None:
    uid, gid = (os.getuid(), os.getgid()) if not RUNNING_AS_ROOT else (1000, 1000)
    assert in_agent("id -u; id -g").split() == [str(uid), str(gid)]
    assert in_agent("whoami")  # the entrypoint added a passwd entry for the host UID
    status = in_agent("grep -E '^(CapEff|CapPrm|NoNewPrivs)' /proc/self/status")
    assert "CapEff:\t0000000000000000" in status
    assert "CapPrm:\t0000000000000000" in status
    assert "NoNewPrivs:\t1" in status
    for tool in ("sh", "git", "claude"):
        assert in_agent(f"command -v {tool}")
    assert "Claude Code" in in_agent("claude --version")
    assert in_agent("test ! -e /var/run/docker.sock && test ! -e /run/docker.sock && echo ok")
    binds = docker("inspect", "-f", "{{range .Mounts}}{{.Source}}\n{{end}}", CONTAINER)
    assert "docker.sock" not in binds


def test_agent_sees_the_workspace_and_git_dirs_but_not_the_main_working_tree(
    env: Env, api: Path
) -> None:
    assert in_agent("pwd") == f"/workspace/{SET}"
    assert in_agent("test -f api/.sandbox/compose.yaml && echo ok") == "ok"
    assert in_agent(f"test -d {api}/.git && echo ok") == "ok"
    assert in_agent(f"test ! -e {api}/.sandbox && echo ok") == "ok"
    # The plan pins the agent override, so the agent can read it but not change it.
    assert in_agent("test -r sandbox.yaml && echo ok") == "ok"
    assert in_agent("echo x >> sandbox.yaml 2>/dev/null || echo refused") == "refused"
    assert in_agent("rm -f sandbox.yaml 2>/dev/null || echo refused") == "refused"


def test_host_claude_dir_is_shared(env: Env, api: Path) -> None:
    (Path.home() / ".claude" / "from-host").write_text("hi")
    assert in_agent('echo "$CLAUDE_CONFIG_DIR"') == "/home/agent/.claude"
    assert in_agent("cat /home/agent/.claude/from-host") == "hi"
    assert in_agent("test ! -e /home/agent/.claude.json && echo ok") == "ok"


@pytest.mark.skipif(
    RUNNING_AS_ROOT, reason="the agent never runs as root, so it can't write root's files"
)
def test_git_works_in_the_worktree_and_files_stay_the_developers(env: Env, api: Path) -> None:
    worktree = f"/workspace/{SET}/api"
    assert in_agent("git branch --show-current", workdir=worktree) == f"agent/{SET}"
    in_agent(
        "echo change > made-by-agent.txt && git add made-by-agent.txt"
        " && git -c user.name=agent -c user.email=agent@example.invalid"
        " commit --quiet -m 'from the agent'",
        workdir=worktree,
    )
    in_agent("echo hi > /home/agent/.claude/written-by-agent")

    on_host = env.set_dir(SET) / "api" / "made-by-agent.txt"
    for path in (on_host, Path.home() / ".claude" / "written-by-agent"):
        assert (path.stat().st_uid, path.stat().st_gid) == (os.getuid(), os.getgid())
    # The commit landed in the source repo's shared object store on the host.
    assert git(api, "log", "-1", "--format=%s", f"agent/{SET}") == "from the agent"
