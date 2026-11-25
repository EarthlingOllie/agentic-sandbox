"""`status` against real Docker labels and the agent's real git clones."""

import json
import shutil
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
from tests.integration.conftest import container_address, git, remove_project, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("status")


def compose_down() -> None:
    remove_project(project_name(SET))


@pytest.fixture
def mysql_repo(env: Env) -> Iterator[Path]:
    path = env.cwd / "api"
    shutil.copytree(FIXTURES / "mysql-repo", path)
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    compose_down()
    yield path
    compose_down()


def run(*args: str) -> Any:  # noqa: ANN401 - parsed JSON
    result = invoke([*args], SubprocessGit(), SubprocessCompose(), SubprocessDocker())
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout) if "--json" in args else result.stdout


def listed_state(name: str) -> str:
    sets = {entry["name"]: entry for entry in run("status", "--json")["sets"]}
    return sets[name]["state"]


def test_status_follows_a_set_through_its_lifecycle(env: Env, mysql_repo: Path) -> None:
    run(SET, "up", "--repos", "api")

    detail = run(SET, "status", "--json")
    assert detail["state"] == "running"
    assert listed_state(SET) == "running"
    services = {s["name"]: s for s in detail["services"]}
    assert set(services) == {"agent", "mysql"}
    assert services["mysql"] == {
        "name": "mysql",
        "state": "running",
        "health": "healthy",
        "hostname": f"mysql.{SET}.test",
        # The container's current address, as Docker has it.
        "ip": container_address(SET, "mysql"),
    }
    assert services["agent"]["ip"] == container_address(SET, "agent")
    assert container_address(SET, "mysql") in run(SET, "status")
    assert detail["repos"] == [
        {
            "name": "api",
            "path": str(env.set_dir(SET) / "api"),
            "exists": True,
            "branch": f"agent/{SET}",
            "modified": False,
            "untracked": False,
            "sync": "unchanged",
            "sync_detail": "",
        }
    ]

    clone = env.set_dir(SET) / "api"
    (clone / "README.md").write_text("changed\n")
    (clone / "notes.txt").write_text("new\n")
    repo = run(SET, "status", "--json")["repos"][0]
    assert (repo["modified"], repo["untracked"]) == (True, True)
    assert "* uncommitted changes, ? untracked files" in run(SET, "status")

    subprocess.run(
        ["docker", "stop", f"{project_name(SET)}-mysql-1"], check=True, capture_output=True
    )
    assert listed_state(SET) == "partial"
    partial = {s["name"]: s for s in run(SET, "status", "--json")["services"]}
    # A stopped container has no address; the one still running keeps its own.
    assert (partial["mysql"]["state"], partial["mysql"]["ip"]) == ("exited", None)
    assert partial["agent"]["ip"] == container_address(SET, "agent")

    run(SET, "down")
    assert listed_state(SET) == "stopped"
    stopped = run(SET, "status", "--json")["services"]
    assert {s["name"]: s["hostname"] for s in stopped} == {
        "agent": f"agent.{SET}.test",
        "mysql": f"mysql.{SET}.test",
    }
    assert [s["ip"] for s in stopped] == [None, None]

    # The set directory goes away: with no containers left either, nothing remains of it.
    shutil.rmtree(env.set_dir(SET))
    assert SET not in {entry["name"] for entry in run("status", "--json")["sets"]}
