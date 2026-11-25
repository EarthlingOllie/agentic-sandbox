"""Two sets run the same `mysql:3306` side by side, publishing nothing: each answers on its
conventional port at its own container's address."""

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
from tests.integration.conftest import (
    container_address,
    git,
    published_ports,
    resolve,
    unique_set,
)

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SETS = (unique_set("net-a"), unique_set("net-b"))
MYSQL_PORT = 3306


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def down_all() -> None:
    for name in SETS:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project_name(name),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            capture_output=True,
            check=False,
        )


@pytest.fixture
def mysql_repo(env: Env) -> Iterator[Path]:
    path = env.cwd / "api"
    shutil.copytree(FIXTURES / "mysql-repo", path)
    compose = path / ".sandbox" / "compose.yaml"
    # Host-side bindings every set would clash on, if sandbox kept them: on every
    # interface, and on loopback under another port number.
    compose.write_text(
        compose.read_text().replace(
            "    image: mysql:8.4\n",
            "    image: mysql:8.4\n    ports:\n"
            '      - "0.0.0.0:3306:3306"\n      - "127.0.0.1:13306:3306"\n',
        )
    )
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    down_all()
    yield path
    down_all()


def mysql_greeting(address: str) -> bytes:
    """Connect to MySQL's conventional port and read the handshake the server opens with."""
    with socket.create_connection((address, MYSQL_PORT), timeout=5) as connection:
        connection.settimeout(5)
        return connection.recv(256)


def test_two_sets_with_the_same_service_both_answer_on_its_conventional_port(
    mysql_repo: Path,
) -> None:
    first, second = SETS
    results = [
        invoke([name, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())
        for name in SETS
    ]

    for result, name in zip(results, SETS, strict=True):
        assert result.exit_code == 0, result.output
        # What `up` tells the developer: the set hostname and the conventional port.
        assert f"    mysql.{name}.test  3306/tcp\n" in result.stdout
        assert "13306" not in result.stdout
    addresses = [container_address(name, "mysql") for name in SETS]
    assert addresses[0] != addresses[1]
    for name, address in zip(SETS, addresses, strict=True):
        # Nothing is published: neither of the repo's two host-side bindings survived.
        assert published_ports(name, "mysql") == ""
        # The set hostname is the container's address, and MySQL answers there on 3306.
        assert resolve(f"mysql.{name}.test") == (address,)
        assert b"8.4" in mysql_greeting(address)
    # Inside the set, the service answers to its short alias and its set hostname.
    for alias in ("mysql", f"mysql.{first}.test"):
        docker(
            "run", "--rm", "--network", f"{project_name(first)}_default", "mysql:8.4",
            "mysqladmin", "ping", "-h", alias, "-psandbox", "--silent",
        )  # fmt: skip

    # The hostname is the stable address: after down and up it leads to the new container.
    assert invoke([second, "down"], SubprocessGit(), SubprocessCompose()).exit_code == 0
    again = invoke([second, "up"], SubprocessGit(), SubprocessCompose())

    assert again.exit_code == 0, again.output
    (resolved,) = resolve(f"mysql.{second}.test")
    assert resolved == container_address(second, "mysql")
    assert b"8.4" in mysql_greeting(resolved)
    # The other set was not disturbed.
    assert b"8.4" in mysql_greeting(addresses[0])
