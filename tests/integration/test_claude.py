"""`sandbox SET claude` against real Docker: Claude Code is reachable in the agent at the set's
workspace, and a stopped set fails clearly. The `/login` flow needs a real Claude account, so
it is covered by fakes in the unit tests and verified by hand."""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from sandbox.compose.cli import SubprocessCompose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

SET = "it-claude"
PROJECT = f"sandbox-{SET}"


def compose_down() -> None:
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


def test_claude_code_runs_in_the_agent_and_a_stopped_set_fails_clearly(
    env: Env, make_repo: Callable[[str], Path]
) -> None:
    make_repo("api")
    git, compose = SubprocessGit(), SubprocessCompose()
    try:
        result = invoke([SET, "up", "--repos", "api"], git, compose)
        assert result.exit_code == 0, result.output
        # Smoke test: what `sandbox SET claude` execs, in the directory it starts in.
        version = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                PROJECT,
                "exec",
                "-T",
                "-w",
                f"/workspace/{SET}",
                "agent",
                "claude",
                "--version",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "Claude Code" in version

        assert invoke([SET, "down"], git, compose).exit_code == 0
        stopped = invoke([SET, "claude"], git, compose)

        assert stopped.exit_code == 1
        assert f"set {SET!r} is not running" in stopped.stderr
    finally:
        compose_down()
