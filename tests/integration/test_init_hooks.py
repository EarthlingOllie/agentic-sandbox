"""Init hooks against real Docker Compose: a hook creates its database; a failing one rolls back."""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("hooks")
PROJECT = project_name(SET)
MYSQL = f"{PROJECT}-mysql-1"
API_HOOK = 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS api_app"\n'


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def fixture_repo(env: Env, fixture: str, name: str, hooks: dict[str, str] | None = None) -> Path:
    path = env.cwd / name
    shutil.copytree(FIXTURES / fixture, path)
    for service, script in (hooks or {}).items():
        (path / ".sandbox" / "init").mkdir(exist_ok=True)
        (path / ".sandbox" / "init" / f"{service}.sh").write_text(script)
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    return path


def databases() -> list[str]:
    query = ["mysql", "-uroot", "-psandbox", "-N", "-e", "SHOW DATABASES"]
    return docker("exec", MYSQL, *query).split()


@pytest.fixture
def cleanup() -> Iterator[None]:
    yield
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


def test_hook_creates_its_database_in_the_shared_service(env: Env, cleanup: None) -> None:
    fixture_repo(env, "shared-mysql-a", "api", {"mysql": API_HOOK})
    fixture_repo(env, "shared-mysql-b", "web")

    result = invoke([SET, "up", "--repos", "api", "web"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    assert {"api_app", "web_app"} <= set(databases())
    # Mounted read-only at /sandbox/init/<repo>/.
    mounted = docker("exec", MYSQL, "ls", "/sandbox/init/api", "/sandbox/init/web")
    assert "mysql.sh" in mounted
    denied = subprocess.run(
        ["docker", "exec", MYSQL, "touch", "/sandbox/init/web/new"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert denied.returncode != 0
    assert "Read-only file system" in denied.stderr

    # On a resume (fresh volumes after `down`) the hook runs again and recreates the database.
    assert invoke([SET, "down"], SubprocessGit(), SubprocessCompose()).exit_code == 0
    result = invoke([SET, "up"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    assert "resumed" in result.stdout
    assert "web_app" in databases()


def test_failing_hook_rolls_the_set_back_and_shows_its_output(env: Env, cleanup: None) -> None:
    failing = "echo 'seed data missing' >&2\nexit 7\n"
    fixture_repo(env, "shared-mysql-a", "api", {"mysql": failing})
    fixture_repo(env, "shared-mysql-b", "web")

    result = invoke([SET, "up", "--repos", "api", "web"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 1
    assert "init hook api/.sandbox/init/mysql.sh failed in service 'mysql' (exit 7)" in (
        result.stderr
    )
    assert "seed data missing" in result.stderr
    assert "rolled back" in result.stderr
    project = f"label=com.docker.compose.project={PROJECT}"
    assert docker("ps", "-a", "--filter", project, "-q") == ""
    assert (env.set_dir(SET) / "sandbox.yaml").exists()
