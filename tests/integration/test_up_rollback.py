"""Rollback, Ctrl-C and the state table against real Docker Compose.

`sandbox` runs as a real process here so Ctrl-C is a real SIGINT, and two sets are
provisioned by two processes at once to check that one set's failure leaves the other alone.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import Env
from tests.integration.conftest import git

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SETS = ("it-rb-ok", "it-rb-bad", "it-rb-int")


def quick_service(healthy: bool) -> str:
    """A service that turns healthy (or unhealthy) within a couple of seconds."""
    check = "true" if healthy else "false"
    return f"""\
services:
  app:
    image: mysql:8.4
    entrypoint: ["sleep", "infinity"]
    stop_grace_period: 1s
    volumes:
      - scratch:/scratch
    healthcheck:
      test: ["CMD", "{check}"]
      interval: 1s
      timeout: 2s
      retries: 2
      start_period: 0s
volumes:
  scratch: {{}}
"""


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def labelled(set_name: str, kind: str) -> list[str]:
    label = f"label=sandbox.set={set_name}"
    if kind == "container":
        return docker("ps", "-a", "--filter", label, "--format", "{{.Names}}").split()
    return docker(kind, "ls", "--filter", label, "--format", "{{.Name}}").split()


def leftovers(set_name: str) -> list[str]:
    return [
        name for kind in ("container", "network", "volume") for name in labelled(set_name, kind)
    ]


def sandbox(env: Env, *args: str, new_session: bool = False) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "sandbox", *args],
        cwd=env.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=new_session,
    )


def commit_repo(path: Path) -> Path:
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    return path


def make_repo(env: Env, name: str, compose: str) -> Path:
    path = env.make_repo_dir(name)
    (path / ".sandbox" / "compose.yaml").write_text(compose)
    return commit_repo(path)


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    for set_name in SETS:
        subprocess.run(
            ["docker", "compose", "-p", f"sandbox-{set_name}", "down", "--volumes"],
            capture_output=True,
            check=False,
        )


def test_parallel_sets_fail_independently_and_rollback_keeps_worktrees(env: Env) -> None:
    make_repo(env, "good", quick_service(healthy=True))
    make_repo(env, "bad", quick_service(healthy=False))

    ok = sandbox(env, "it-rb-ok", "up", "--repos", "good")
    bad = sandbox(env, "it-rb-bad", "up", "--repos", "bad")
    _, ok_err = ok.communicate(timeout=180)
    _, bad_err = bad.communicate(timeout=180)

    assert ok.returncode == 0, ok_err
    assert bad.returncode == 1
    assert "rolled back set 'it-rb-bad'" in bad_err
    assert sorted(labelled("it-rb-ok", "container")) == [
        "sandbox-it-rb-ok-agent-1",
        "sandbox-it-rb-ok-app-1",
    ]
    assert leftovers("it-rb-bad") == []
    worktree = env.set_dir("it-rb-bad") / "bad"
    assert git(worktree, "branch", "--show-current") == "agent/it-rb-bad"
    assert (env.set_dir("it-rb-bad") / "sandbox.yaml").is_file()


def test_running_set_is_a_no_op_and_a_stopped_one_resumes(env: Env) -> None:
    make_repo(env, "good", quick_service(healthy=True))
    first = sandbox(env, "it-rb-ok", "up", "--repos", "good")
    assert first.wait(timeout=180) == 0
    # The app's container (the set's agent runs next to it).
    app = ["ps", "-q", "--filter", "label=sandbox.set=it-rb-ok"]
    app += ["--filter", "label=com.docker.compose.service=app"]
    container_id = docker(*app)

    again = sandbox(env, "it-rb-ok", "up")
    out, err = again.communicate(timeout=60)
    assert again.returncode == 0, err
    assert "already running" in out
    assert docker(*app) == container_id

    docker("stop", "-t", "1", container_id)
    resumed = sandbox(env, "it-rb-ok", "up")
    out, err = resumed.communicate(timeout=180)
    assert resumed.returncode == 0, err
    assert "resumed" in out

    recreated = sandbox(env, "it-rb-ok", "up", "--recreate")
    out, err = recreated.communicate(timeout=180)
    assert recreated.returncode == 0, err
    assert "recreated" in out
    assert docker(*app) != container_id


@pytest.mark.parametrize("whole_group", [True, False], ids=["terminal-ctrl-c", "sigint-to-cli"])
def test_ctrl_c_mid_up_leaves_no_orphaned_resources(env: Env, whole_group: bool) -> None:
    # mysql takes several seconds to turn healthy, so `up` is still waiting when we interrupt.
    path = env.cwd / "api"
    shutil.copytree(FIXTURES / "mysql-repo", path)
    commit_repo(path)

    process = sandbox(env, "it-rb-int", "up", "--repos", "api", new_session=True)
    deadline = time.monotonic() + 120
    while not labelled("it-rb-int", "container"):
        assert process.poll() is None, process.communicate()
        assert time.monotonic() < deadline, "the container never appeared"
        time.sleep(0.2)
    if whole_group:
        os.killpg(process.pid, signal.SIGINT)  # what a terminal does on Ctrl-C
    else:
        process.send_signal(signal.SIGINT)  # only sandbox: it must stop `compose up` itself
    _, err = process.communicate(timeout=120)

    assert process.returncode == 130, err
    assert "interrupted" in err
    assert "rolled back set 'it-rb-int'" in err
    assert leftovers("it-rb-int") == []
    assert (env.set_dir("it-rb-int") / "api" / ".git").is_file()
