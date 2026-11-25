"""`urls` against real Docker Compose: a running set, then the same set stopped."""

import json
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.docker import SubprocessDocker
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import container_address, git, resolve, unique_set

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
    # Declare the port (behind another host-side number), so the set lists mysql as
    # host:port on its conventional port.
    compose = path / ".sandbox" / "compose.yaml"
    compose.write_text(
        compose.read_text().replace(
            "    image: mysql:8.4\n", '    image: mysql:8.4\n    ports: ["13306:3306"]\n'
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


def test_urls_of_a_running_then_stopped_set(env: Env, mysql_repo: Path) -> None:
    run(SET, "up", "--repos", "api")
    address = container_address(SET, "mysql")
    mysql = {"address": f"mysql.{SET}.test:3306", "service": "mysql", "ip": address}

    running = json.loads(run(SET, "urls", "--json"))
    assert (running["state"], running["running"]) == ("running", True)
    assert (running["web"], running["services"]) == ([], [mysql])
    # The address listed is one a client can use: MySQL accepts connections there.
    host, _, port = mysql["address"].rpartition(":")
    assert resolve(host) == (address,)
    with socket.create_connection((address, int(port)), timeout=5):
        pass

    run(SET, "down")

    stopped = json.loads(run(SET, "urls", "--json"))
    assert (stopped["state"], stopped["running"]) == ("stopped", False)
    assert stopped["services"] == [{**mysql, "ip": None}]
    assert "not running" in run(SET, "urls")
