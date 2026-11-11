"""`status` against real Docker labels and the agent's real git clones."""

import json
import shutil
import subprocess
from collections.abc import Iterator
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.docker import SubprocessDocker
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git, ip_in_block, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("status")


def compose_down() -> None:
    subprocess.run(
        ["docker", "compose", "-p", project_name(SET), "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


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


def test_status_follows_a_set_through_its_lifecycle(
    env: Env, mysql_repo: Path, ip_pool: IPv4Network
) -> None:
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
        "ip": ip_in_block(ip_pool, 0, 1),
    }
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

    run(SET, "down")
    assert listed_state(SET) == "stopped"

    # The set directory goes away; its sticky lease is a leftover.
    shutil.rmtree(env.set_dir(SET))
    sets = {entry["name"]: entry for entry in run("status", "--json")["sets"]}
    assert sets[SET]["state"] == "orphaned"
    assert "lease" in sets[SET]["detail"]
