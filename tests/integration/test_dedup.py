"""Service dedup against real `docker compose config`, and one multi-repo `up` via the CLI."""

import asyncio
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.compose.dedup import ServiceConflictError, resolve
from sandbox.compose.services import RepoCompose, load_repo_compose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("dedup")
PROJECT = project_name(SET)


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def fixture_repo(env: Env, fixture: str, name: str) -> Path:
    path = env.cwd / name
    shutil.copytree(FIXTURES / fixture, path)
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    return path


def repo_compose(name: str, path: Path) -> RepoCompose:
    loaded = load_repo_compose(name, path)
    assert loaded is not None
    return loaded


@pytest.fixture
def cleanup() -> Iterator[None]:
    yield
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


def test_normalisation_makes_extends_and_list_environment_match(env: Env) -> None:
    api = repo_compose("api", fixture_repo(env, "shared-mysql-a", "api"))
    web = repo_compose("web", fixture_repo(env, "shared-mysql-b", "web"))

    graph = asyncio.run(resolve(SubprocessCompose(), PROJECT, [api, web]))

    assert graph.names() == ["mysql", "toolbox"]
    assert graph.services["mysql"].repos == ("api", "web")
    assert graph.services["mysql"].definition["healthcheck"]["interval"] == "2s"


def test_conflicting_definitions_are_reported_with_a_diff(env: Env) -> None:
    api = repo_compose("api", fixture_repo(env, "shared-mysql-a", "api"))
    old = repo_compose("old", fixture_repo(env, "conflict-mysql", "old"))

    with pytest.raises(ServiceConflictError) as caught:
        asyncio.run(resolve(SubprocessCompose(), PROJECT, [api, old]))

    message = str(caught.value)
    assert "service 'mysql' is defined differently by api and old" in message
    assert "-image: mysql:8.4" in message
    assert "+image: mysql:8.0" in message


def test_multi_repo_up_shares_one_mysql(env: Env, cleanup: None) -> None:
    fixture_repo(env, "shared-mysql-a", "api")
    fixture_repo(env, "shared-mysql-b", "web")

    result = invoke([SET, "up", "--repos", "api", "web"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    names = docker("ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
                   "--format", "{{.Names}}").split()  # fmt: skip
    # One mysql for both repos, beside web's toolbox and the set's agent.
    assert sorted(names) == [f"{PROJECT}-agent-1", f"{PROJECT}-mysql-1", f"{PROJECT}-toolbox-1"]
    # The healthcheck only api declares was kept, and `up --wait` waited for it.
    health = docker("inspect", "-f", "{{.State.Health.Status}}", f"{PROJECT}-mysql-1")
    assert health == "healthy"
    # web's relative paths resolved from web's repo, though api's file came first.
    toolbox = f"{PROJECT}-toolbox-1"
    assert docker("exec", toolbox, "cat", "/marker.txt") == "resolved from shared-mysql-b"
    assert docker("exec", toolbox, "printenv", "MARKER") == "from-shared-mysql-b"


def test_conflicting_repos_fail_up_before_any_container_starts(env: Env, cleanup: None) -> None:
    fixture_repo(env, "shared-mysql-a", "api")
    fixture_repo(env, "conflict-mysql", "old")

    result = invoke([SET, "up", "--repos", "api", "old"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 1
    assert "defined differently by api and old" in result.stderr
    assert docker("ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}", "-q") == ""
    assert not env.set_dir(SET).exists()
