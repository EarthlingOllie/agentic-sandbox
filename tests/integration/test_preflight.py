"""`up`'s prerequisite checks against real Docker.

The checks themselves are unit-tested with the fake host and fake Docker
(``tests/unit/test_preflight.py``). Here: the daemon really names what runs it, which is
what the macOS check reads, and a refused `up` really leaves nothing in Docker.
"""

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.docker import SubprocessDocker
from sandbox.git import SubprocessGit
from sandbox.platforms import Host
from tests.conftest import Env, invoke
from tests.integration.conftest import git, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

SET = unique_set("unreachable")
COMPOSE = "services:\n  app:\n    image: python:3.12-alpine\n    command: sleep infinity\n"


def test_the_docker_daemon_names_what_runs_it() -> None:
    name = asyncio.run(SubprocessDocker().operating_system())

    assert name
    assert "\n" not in name


def test_a_refused_up_leaves_nothing_in_docker(
    env: Env, make_repo: Callable[[str], Path], tmp_path: Path
) -> None:
    repo = make_repo("api")
    (repo / ".sandbox" / "compose.yaml").write_text(COMPOSE)
    git(repo, "commit", "--quiet", "-am", "a service")
    # A machine setup pointed at the resolver for another domain than the configured one.
    machine = tmp_path / "another-machine"
    drop_in = machine / "etc" / "systemd" / "resolved.conf.d" / "sandbox.conf"
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text("[Resolve]\nDNS=127.0.0.1:15353\nDomains=~elsewhere.example\n")

    result = invoke(
        [SET, "up", "--repos", "api"],
        SubprocessGit(),
        SubprocessCompose(),
        host=Host(platform="linux", root=machine),
    )

    assert result.exit_code == 1
    assert "the configured domain is `test`" in result.output
    assert "resolver for `elsewhere.example`" in result.output
    assert not env.set_dir(SET).exists()
    for kind in ("container", "network", "volume"):
        listed = subprocess.run(
            ["docker", kind, "ls", "--quiet", "--filter", f"name={project_name(SET)}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert listed.strip() == "", f"{kind}: {listed}"
