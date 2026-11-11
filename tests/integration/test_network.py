"""Two sets run the same `mysql:3306` side by side, each on its own leased loopback IP."""

import hashlib
import re
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
from tests.integration.conftest import RUN_ID, git, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SETS = (unique_set("net-a"), unique_set("net-b"))
# A pool of this run's own, so its bindings can't meet another run's on the host.
OCTET = 64 + int(hashlib.sha256(RUN_ID.encode()).hexdigest(), 16) % 128
POOL = f"127.{OCTET}.0.0/16"


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
    with env.config_file.open("a") as config:
        config.write(f"ip_pool: {POOL}\n")
    path = env.cwd / "api"
    shutil.copytree(FIXTURES / "mysql-repo", path)
    compose = path / ".sandbox" / "compose.yaml"
    # A host-side binding every set would clash on, if sandbox kept it.
    compose.write_text(
        compose.read_text().replace(
            "    image: mysql:8.4\n", '    image: mysql:8.4\n    ports:\n      - "3306:3306"\n'
        )
    )
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    down_all()
    yield path
    down_all()


def binding(output: str, set_name: str) -> str:
    match = re.search(rf"mysql\.{re.escape(set_name)}\.test  (\S+)  3306/tcp", output)
    assert match, output
    return match.group(1)


def reachable(ip: str, port: int) -> bool:
    with socket.create_connection((ip, port), timeout=5):
        return True


def test_two_sets_with_the_same_service_come_up_side_by_side(mysql_repo: Path) -> None:
    first, second = SETS
    results = [
        invoke([name, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())
        for name in SETS
    ]

    for result in results:
        assert result.exit_code == 0, result.output
    ips = [binding(result.stdout, name) for result, name in zip(results, SETS, strict=True)]
    assert ips == [f"127.{OCTET}.0.1", f"127.{OCTET}.1.1"]
    for name, ip in zip(SETS, ips, strict=True):
        # Published on the hostname's IP only, at the native port.
        assert docker("port", f"{project_name(name)}-mysql-1", "3306/tcp") == f"{ip}:3306"
        assert reachable(ip, 3306)
    # Inside the set, the service answers to its short alias and its set hostname.
    for alias in ("mysql", f"mysql.{first}.test"):
        docker(
            "run", "--rm", "--network", f"{project_name(first)}_default", "mysql:8.4",
            "mysqladmin", "ping", "-h", alias, "-psandbox", "--silent",
        )  # fmt: skip

    # Sticky: down then up hands the set the same IP.
    assert invoke([second, "down"], SubprocessGit(), SubprocessCompose()).exit_code == 0
    again = invoke([second, "up"], SubprocessGit(), SubprocessCompose())

    assert again.exit_code == 0, again.output
    assert binding(again.stdout, second) == ips[1]
