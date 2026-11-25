"""A set an earlier version created keeps working: `down` and `up` are the whole migration.

That version gave each service a loopback address of its own, published the service's ports
there (in the set's ``override.yaml``), and recorded the address in a lease file under the
state directory. The test puts a running set back into exactly that state (checked against
a set the earlier version really created: same override entry, same published port, same
lease file, and no resolver on the set's network), then runs `down` and `up`.
"""

import json
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.compose.resolver import RESOLVER_CONTAINER, set_network
from sandbox.docker import SubprocessDocker
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import (
    RUN_ID,
    container_address,
    git,
    published_ports,
    remove_project,
    resolve,
    resolver_networks,
    unique_set,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="publishes on a 127.x address"),
]

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("upgrade")
MYSQL_PORT = 3306
# The address the earlier version would have leased: this run's own, so two runs on one
# machine never publish the same address and port. Never 127.0 (localhost).
LEASED = f"127.{64 + int(RUN_ID[:2], 16) % 184}.{int(RUN_ID[2:4], 16)}.1"


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def sandbox(*args: str) -> str:
    result = invoke([*args], SubprocessGit(), SubprocessCompose(), SubprocessDocker())
    assert result.exit_code == 0, result.output
    return result.stdout


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
    remove_project(project_name(SET))
    yield path
    remove_project(project_name(SET))


def leave_as_the_earlier_version_did(env: Env) -> Path:
    """Turn the running set into what the earlier version left running; returns the lease
    file it kept."""
    project = project_name(SET)
    mysql = f"{project}-mysql-1"
    label = '{{index .Config.Labels "com.docker.compose.project.%s"}}'
    files = docker("container", "inspect", "--format", label % "config_files", mysql).split(",")
    directory = docker("container", "inspect", "--format", label % "working_dir", mysql)
    # Its override published each port on the service's leased address.
    override = env.set_dir(SET) / "override.yaml"
    text = override.read_text()
    assert "    ports: !override []\n" in text
    override.write_text(
        text.replace(
            "    ports: !override []\n",
            f"    ports: !override\n    - {LEASED}:{MYSQL_PORT}:{MYSQL_PORT}/tcp\n",
            1,
        )
    )
    # It knew no resolver.
    docker("network", "disconnect", "--force", set_network(SET), RESOLVER_CONTAINER)
    compose_files = [argument for file in files for argument in ("-f", file)]
    docker(
        "compose", "-p", project, "--project-directory", directory, *compose_files,
        "up", "--detach", "--wait",
    )  # fmt: skip
    # And it kept the lease under the state directory.
    leases = env.state_dir / "leases.json"
    leases.parent.mkdir(parents=True, exist_ok=True)
    block = LEASED.rsplit(".", 1)[0] + ".0/24"
    leases.write_text(
        json.dumps(
            {"sets": {SET: {"block": block, "ips": {f"mysql.{SET}.test": LEASED}}}}, indent=2
        )
    )
    return leases


def mysql_greeting(address: str) -> bytes:
    with socket.create_connection((address, MYSQL_PORT), timeout=5) as connection:
        connection.settimeout(5)
        return connection.recv(256)


def test_a_set_the_earlier_version_created_is_reachable_after_down_and_up(
    env: Env, mysql_repo: Path
) -> None:
    sandbox(SET, "up", "--repos", "api")
    leases = leave_as_the_earlier_version_did(env)
    lease_text = leases.read_text()
    # As that version left it: published on the leased address, unknown to the resolver.
    assert f"{LEASED}:{MYSQL_PORT}" in published_ports(SET, "mysql")
    assert b"8.4" in mysql_greeting(LEASED)
    assert set_network(SET) not in resolver_networks()
    assert json.loads(sandbox("status", "--json")) == {
        "sets": [{"name": SET, "state": "running", "detail": ""}]
    }

    sandbox(SET, "down")
    resumed = sandbox(SET, "up")

    assert f"    mysql.{SET}.test  3306/tcp\n" in resumed
    # Nothing is published any more, on the leased address or anywhere else.
    assert published_ports(SET, "mysql") == ""
    with pytest.raises(OSError):  # noqa: PT011 - refused or unreachable, either is "gone"
        mysql_greeting(LEASED)
    # The set hostname is the container's address, and MySQL answers there on 3306.
    (resolved,) = resolve(f"mysql.{SET}.test")
    assert resolved == container_address(SET, "mysql")
    assert b"8.4" in mysql_greeting(resolved)
    urls = json.loads(sandbox(SET, "urls", "--json"))
    assert urls["services"] == [
        {"address": f"mysql.{SET}.test:3306", "service": "mysql", "ip": resolved}
    ]
    # The lease is not a set, and nobody touched it.
    assert json.loads(sandbox("status", "--json")) == {
        "sets": [{"name": SET, "state": "running", "detail": ""}]
    }
    assert leases.read_text() == lease_text
