"""`up` and `down` against real Docker Compose, using the `mysql-repo` fixture."""

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
SET = unique_set("compose")
PROJECT = project_name(SET)
LABEL = f"sandbox.set={SET}"


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def labelled(kind: str) -> list[str]:
    """Names of containers/networks/volumes carrying this set's label."""
    if kind == "container":
        return docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}").split()
    return docker(kind, "ls", "--filter", f"label={LABEL}", "--format", "{{.Name}}").split()


@pytest.fixture
def mysql_repo(env: Env) -> Iterator[Path]:
    path = env.cwd / "api"
    shutil.copytree(FIXTURES / "mysql-repo", path)
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    yield path
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


def test_up_waits_for_healthy_services_and_down_removes_them(env: Env, mysql_repo: Path) -> None:
    result = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    # `up` returned, so the service is already healthy: no polling here.
    container = f"{PROJECT}-mysql-1"
    assert docker("inspect", "-f", "{{.State.Health.Status}}", container) == "healthy"
    assert sorted(labelled("container")) == [f"{PROJECT}-agent-1", container]
    assert labelled("network") == [f"{PROJECT}_default"]
    assert labelled("volume") == [f"{PROJECT}_data"]
    # Reachable by its short alias from another container on the set's network.
    docker(
        "run", "--rm", "--network", f"{PROJECT}_default", "mysql:8.4",
        "mysqladmin", "ping", "-h", "mysql", "-psandbox", "--silent",
    )  # fmt: skip
    # The relative bind mount resolved from the repo, not from the set directory.
    query = ["mysql", "-uroot", "-psandbox", "-N", "-e", "SELECT @@max_connections"]
    assert docker("exec", container, *query) == "77"

    result = invoke([SET, "down"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    assert labelled("container") == []
    assert labelled("network") == []
    assert labelled("volume") == []
    clone = env.set_dir(SET) / "api"
    assert git(clone, "branch", "--show-current") == f"agent/{SET}"
    assert (clone / ".sandbox" / "compose.yaml").is_file()


def test_repo_without_compose_file_provisions_only_its_clone(env: Env, mysql_repo: Path) -> None:
    (mysql_repo / ".sandbox" / "compose.yaml").unlink()
    git(mysql_repo, "commit", "--quiet", "-am", "no services")

    result = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    assert git(env.set_dir(SET) / "api", "branch", "--show-current") == f"agent/{SET}"
    # No backing services: only the set's agent runs.
    assert labelled("container") == [f"{PROJECT}-agent-1"]
