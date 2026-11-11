"""`urls` against real Docker Compose: a running set, then the same set stopped."""

import json
import shutil
import subprocess
from collections.abc import Iterator
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.docker import SubprocessDocker
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git, ip_in_block, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("urls")


def compose_down() -> None:
    subprocess.run(
        ["docker", "compose", "-p", project_name(SET), "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


@pytest.fixture
def mysql_repo(env: Env) -> Iterator[Path]:
    path = env.cwd / "api"
    shutil.copytree(FIXTURES / "mysql-repo", path)
    # Publish the native port, so the set lists mysql as host:port.
    compose = path / ".sandbox" / "compose.yaml"
    compose.write_text(
        compose.read_text().replace(
            "    image: mysql:8.4\n", '    image: mysql:8.4\n    ports: ["3306:3306"]\n'
        )
    )
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    compose_down()
    yield path
    compose_down()


def run(*args: str) -> Any:  # noqa: ANN401 - parsed JSON or the table
    result = invoke([*args], SubprocessGit(), SubprocessCompose(), SubprocessDocker())
    assert result.exit_code == 0, result.output
    return result.stdout


def test_urls_of_a_running_then_stopped_set(
    env: Env, mysql_repo: Path, ip_pool: IPv4Network
) -> None:
    run(SET, "up", "--repos", "api")
    mysql = {
        "address": f"mysql.{SET}.test:3306",
        "service": "mysql",
        "ip": ip_in_block(ip_pool, 0, 1),
    }

    running = json.loads(run(SET, "urls", "--json"))
    assert (running["state"], running["running"]) == ("running", True)
    assert (running["web"], running["services"]) == ([], [mysql])

    run(SET, "down")

    stopped = json.loads(run(SET, "urls", "--json"))
    assert (stopped["state"], stopped["running"]) == ("stopped", False)
    assert stopped["services"] == [mysql]
    assert "not running" in run(SET, "urls")
