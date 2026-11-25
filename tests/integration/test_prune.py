"""A real `prune` clears a stopped set's containers, network and volumes, keeps its clone,
and a fresh `up` brings the set back on its conventional port."""

import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import container_address, git, published_ports, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("prune")


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


def host_state(env: Env) -> list[str]:
    """What sandbox keeps outside set directories: the hosts file and non-lock state files."""
    kept = [p for p in env.state_dir.rglob("*") if p.is_file() and p.suffix != ".lock"]
    return sorted(str(p) for p in [*kept, *([env.hosts_file] if env.hosts_file.exists() else [])])


def project_resources(project: str) -> list[str]:
    label = f"label=com.docker.compose.project={project}"
    return [
        *docker("ps", "--all", "--quiet", "--filter", label).split(),
        *docker("network", "ls", "--quiet", "--filter", label).split(),
        *docker("volume", "ls", "--quiet", "--filter", label).split(),
    ]


def test_prune_clears_the_set_and_a_fresh_up_brings_it_back(env: Env, mysql_repo: Path) -> None:
    project = project_name(SET)
    up = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())
    assert up.exit_code == 0, up.output
    # Stopped, not downed: exited containers, the network and the volume are all still
    # there for prune to clear, and the resolver is still on the network.
    docker("compose", "-p", project, "stop")
    assert len(project_resources(project)) >= 3
    state_before = host_state(env)

    result = invoke([SET, "prune"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    assert project_resources(project) == []
    assert (env.set_dir(SET) / "api" / ".git").is_dir()
    assert (env.set_dir(SET) / "sandbox.yaml").is_file()
    # Neither `up` nor `prune` kept any addressing state of its own.
    assert state_before == host_state(env) == []

    again = invoke([SET, "up"], SubprocessGit(), SubprocessCompose())

    assert again.exit_code == 0, again.output
    assert published_ports(SET, "mysql") == ""
    with socket.create_connection((container_address(SET, "mysql"), 3306), timeout=5):
        pass
