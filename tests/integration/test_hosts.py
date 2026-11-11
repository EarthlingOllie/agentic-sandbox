"""A real `up` writes the set's hosts block (a temp hosts file); `down` removes it."""

import shutil
import socket
import subprocess
from collections.abc import Iterator
from ipaddress import IPv4Network
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git, ip_in_block, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("hosts")
OTHER = "127.0.0.1\tlocalhost\n"


def down() -> None:
    subprocess.run(
        ["docker", "compose", "-p", project_name(SET), "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


@pytest.fixture
def mysql_repo(env: Env) -> Iterator[Path]:
    path = env.cwd / "api"
    shutil.copytree(FIXTURES / "mysql-repo", path)
    compose = path / ".sandbox" / "compose.yaml"
    compose.write_text(
        compose.read_text().replace(
            "    image: mysql:8.4\n", '    image: mysql:8.4\n    ports:\n      - "3306:3306"\n'
        )
    )
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    down()
    yield path
    down()


def test_up_writes_the_block_and_down_removes_it(
    env: Env, mysql_repo: Path, ip_pool: IPv4Network
) -> None:
    env.hosts_file.write_text(OTHER)

    result = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    ip = ip_in_block(ip_pool, 0, 1)
    assert env.hosts_file.read_text() == (
        f"{OTHER}# sandbox:{SET}\n{ip}\tmysql.{SET}.test\n# end sandbox:{SET}\n"
    )
    # The address the block names is where the service answers.
    with socket.create_connection((ip, 3306), timeout=5):
        pass

    assert invoke([SET, "down"], SubprocessGit(), SubprocessCompose()).exit_code == 0
    assert env.hosts_file.read_text() == OTHER
