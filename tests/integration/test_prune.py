"""A real `prune` clears a stopped set's containers, network, volumes, hosts block and
lease, keeps its clone, and a fresh `up` leases it a new block."""

import asyncio
import hashlib
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
from sandbox.leases import LeaseStore
from tests.conftest import Env, invoke
from tests.integration.conftest import RUN_ID, git, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("prune")
# A pool of this run's own (distinct from test_network's and test_hosts'), so bindings
# never meet.
OCTET = 8 + int(hashlib.sha256(RUN_ID.encode()).hexdigest(), 16) % 48
POOL = f"127.{OCTET}.0.0/16"


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def down() -> None:
    subprocess.run(
        ["docker", "compose", "-p", project_name(SET), "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


@pytest.fixture
def mysql_repo(env: Env) -> Iterator[Path]:
    with env.config_file.open("a") as config:
        config.write(f"ip_pool: {POOL}\n")
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


def project_resources(project: str) -> list[str]:
    label = f"label=com.docker.compose.project={project}"
    return [
        *docker("ps", "--all", "--quiet", "--filter", label).split(),
        *docker("network", "ls", "--quiet", "--filter", label).split(),
        *docker("volume", "ls", "--quiet", "--filter", label).split(),
    ]


def test_prune_clears_the_set_and_a_fresh_up_gets_a_new_block(env: Env, mysql_repo: Path) -> None:
    project = project_name(SET)
    leases = LeaseStore(
        env.state_dir / "leases.json", env.state_dir / "locks" / "leases.lock", IPv4Network(POOL)
    )
    up = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())
    assert up.exit_code == 0, up.output
    # Stopped, not downed: exited containers, the network, the volume and the hosts block
    # are all still there for prune to clear.
    docker("compose", "-p", project, "stop")
    assert len(project_resources(project)) >= 3
    assert f"# sandbox:{SET}" in env.hosts_file.read_text()
    first = leases.read(SET)
    assert first is not None
    assert first.block == IPv4Network(f"127.{OCTET}.0.0/24")

    result = invoke([SET, "prune"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    assert project_resources(project) == []
    assert leases.read(SET) is None
    assert f"# sandbox:{SET}" not in env.hosts_file.read_text()
    assert (env.set_dir(SET) / "api" / ".git").is_dir()
    assert (env.set_dir(SET) / "sandbox.yaml").is_file()

    # Another set takes the released block, so the fresh lease is visibly a new one.
    asyncio.run(leases.lease("other", ["mysql.other.test"]))
    again = invoke([SET, "up"], SubprocessGit(), SubprocessCompose())

    assert again.exit_code == 0, again.output
    ip = f"127.{OCTET}.1.1"
    second = leases.read(SET)
    assert second is not None
    assert second.block == IPv4Network(f"127.{OCTET}.1.0/24")
    assert f"{ip}\tmysql.{SET}.test" in env.hosts_file.read_text()
    with socket.create_connection((ip, 3306), timeout=5):
        pass
