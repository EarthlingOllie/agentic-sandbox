"""`sandbox status` and `sandbox SET status`: set states reconciled against Docker labels."""

import json
from pathlib import Path
from typing import Any

from sandbox.compose import project_name
from sandbox.docker import Container
from sandbox.git import SyncOutcome, SyncResult
from sandbox.models.status import ServiceStatus
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose
from tests.fakes_docker import FakeDocker

MYSQL_AND_REDIS = """\
services:
  mysql:
    image: mysql:8.4
    ports:
      - "3306:3306"
  redis:
    image: redis:7
"""


def make_repo(env: Env, git: FakeGit, name: str, compose: str | None = None) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    if compose is not None:
        (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def up(set_name: str, git: FakeGit, docker: FakeDocker, *repos: str) -> None:
    result = invoke([set_name, "up", "--repos", *repos], git, docker.compose, docker)
    assert result.exit_code == 0, result.output


def status_json(git: FakeGit, docker: FakeDocker, *args: str) -> Any:  # noqa: ANN401 - JSON
    result = invoke([*args, "status", "--json"], git, docker.compose, docker)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def states(git: FakeGit, docker: FakeDocker) -> dict[str, str]:
    listing = status_json(git, docker)
    return {entry["name"]: entry["state"] for entry in listing["sets"]}


def test_no_sets_prints_a_hint(env: Env) -> None:
    result = invoke(["status"], FakeGit(), FakeCompose(), FakeDocker())

    assert result.exit_code == 0, result.output
    assert "no sets" in result.stdout
    assert status_json(FakeGit(), FakeDocker()) == {"sets": []}


def test_lists_every_set_with_running_stopped_and_partial_states(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "api", MYSQL_AND_REDIS)
    for name in ("alpha", "bravo", "charlie"):
        up(name, git, docker, "api")
    assert invoke(["bravo", "down"], git, docker.compose, docker).exit_code == 0
    docker.compose.stopped_services[project_name("charlie")] = {"redis"}

    assert states(git, docker) == {"alpha": "running", "bravo": "stopped", "charlie": "partial"}
    table = invoke(["status"], git, docker.compose, docker).stdout.splitlines()
    assert table[0].split() == ["SET", "STATE", "DETAIL"]
    assert table[1].split() == ["alpha", "running"]
    assert table[2].split() == ["bravo", "stopped"]
    assert table[3].split()[:2] == ["charlie", "partial"]
    assert "redis" in table[3]


def test_a_set_with_only_some_containers_left_is_partial(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "api", MYSQL_AND_REDIS)
    up("alpha", git, docker, "api")
    # Compose's project is gone, but one container survived (e.g. a crash mid-`down`).
    docker.compose.running.clear()
    docker.extra.append(Container("alpha", "mysql", "running", None))

    assert states(git, docker) == {"alpha": "partial"}


def test_leftovers_without_a_plan_are_orphaned(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    docker.extra.append(Container("ghost", "mysql", "exited", None))
    (env.sets_root / "half").mkdir(parents=True)  # a set directory that never got a plan

    listing = status_json(git, docker)

    by_name = {entry["name"]: entry for entry in listing["sets"]}
    assert {name: entry["state"] for name, entry in by_name.items()} == {
        "ghost": "orphaned",
        "half": "orphaned",
    }
    assert by_name["ghost"]["detail"] == "1 container with no set directory"
    assert by_name["half"]["detail"] == "set directory without a plan"


def test_a_lease_left_by_the_loopback_model_is_not_a_set(env: Env) -> None:
    """Nothing leases IPs any more, so nothing could ever clear such an orphan."""
    env.state_dir.mkdir(parents=True, exist_ok=True)
    (env.state_dir / "leases.json").write_text(
        json.dumps(
            {
                "pool": "127.42.0.0/16",
                "sets": {"old": {"block": 0, "ips": {"mysql.old.test": "127.42.0.1"}}},
            }
        )
    )

    assert status_json(FakeGit(), FakeDocker()) == {"sets": []}


def test_set_status_shows_each_running_services_hostname_and_current_address(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "api", MYSQL_AND_REDIS)
    make_repo(env, git, "web")
    up("jira-123", git, docker, "api", "web")
    docker.health[("jira-123", "mysql")] = "healthy"
    docker.addresses[("jira-123", "mysql")] = "172.20.0.3"
    docker.addresses[("jira-123", "redis")] = "172.20.0.4"
    docker.compose.stopped_services[project_name("jira-123")] = {"redis"}

    detail = status_json(git, docker, "jira-123")

    assert detail["name"] == "jira-123"
    assert detail["state"] == "partial"
    assert detail["project"] == "sandbox-jira-123"
    assert detail["services"] == [
        {"name": "agent", "state": "running", "health": None, "hostname": None, "ip": None},
        {
            "name": "mysql",
            "state": "running",
            "health": "healthy",
            "hostname": "mysql.jira-123.test",
            "ip": "172.20.0.3",
        },
        # A stopped container has no address, whatever it had while it ran.
        {
            "name": "redis",
            "state": "exited",
            "health": None,
            "hostname": "redis.jira-123.test",
            "ip": None,
        },
    ]

    table = invoke(["jira-123", "status"], git, docker.compose, docker).stdout
    assert "set jira-123: partial [sandbox-jira-123]" in table
    rows = [line.split() for line in table.splitlines()]
    assert ["SERVICE", "STATE", "HEALTH", "HOSTNAME", "ADDRESS"] in rows
    assert ["mysql", "running", "healthy", "mysql.jira-123.test", "172.20.0.3"] in rows
    assert ["redis", "exited", "-", "redis.jira-123.test", "-"] in rows


def test_the_address_follows_the_container_across_down_and_up(env: Env) -> None:
    """It is read from Docker each time: nothing remembers the address of the last `up`."""
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "api", MYSQL_AND_REDIS)
    up("jira-123", git, docker, "api")
    docker.addresses[("jira-123", "mysql")] = "172.20.0.3"
    before = {s["name"]: s["ip"] for s in status_json(git, docker, "jira-123")["services"]}
    assert invoke(["jira-123", "down"], git, docker.compose, docker).exit_code == 0
    assert invoke(["jira-123", "up"], git, docker.compose, docker).exit_code == 0
    docker.addresses[("jira-123", "mysql")] = "172.21.0.2"

    after = {s["name"]: s["ip"] for s in status_json(git, docker, "jira-123")["services"]}

    assert (before["mysql"], after["mysql"]) == ("172.20.0.3", "172.21.0.2")


def test_set_status_marks_repos_with_uncommitted_or_untracked_work(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    for name in ("api", "web", "worker"):
        make_repo(env, git, name)
    up("jira-123", git, docker, "api", "web", "worker")
    git.clone(env.set_dir("jira-123") / "api").modified = True
    git.clone(env.set_dir("jira-123") / "web").untracked = True

    detail = status_json(git, docker, "jira-123")

    assert detail["repos"] == [
        {
            "name": "api",
            "path": str(env.set_dir("jira-123") / "api"),
            "exists": True,
            "branch": "agent/jira-123",
            "modified": True,
            "untracked": False,
            "sync": "unchanged",
            "sync_detail": "",
        },
        {
            "name": "web",
            "path": str(env.set_dir("jira-123") / "web"),
            "exists": True,
            "branch": "agent/jira-123",
            "modified": False,
            "untracked": True,
            "sync": "unchanged",
            "sync_detail": "",
        },
        {
            "name": "worker",
            "path": str(env.set_dir("jira-123") / "worker"),
            "exists": True,
            "branch": "agent/jira-123",
            "modified": False,
            "untracked": False,
            "sync": "unchanged",
            "sync_detail": "",
        },
    ]
    rows = [
        line.split()
        for line in invoke(["jira-123", "status"], git, docker.compose, docker).stdout.splitlines()
    ]
    assert ["api", "agent/jira-123", "*", "uncommitted", "changes"] in rows
    assert ["web", "agent/jira-123", "?", "untracked", "files"] in rows
    assert ["worker", "agent/jira-123", "clean"] in rows


def test_set_status_of_a_stopped_set_shows_hostnames_without_addresses(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "api", MYSQL_AND_REDIS)
    up("jira-123", git, docker, "api")
    docker.addresses[("jira-123", "mysql")] = "172.20.0.3"
    assert invoke(["jira-123", "down"], git, docker.compose, docker).exit_code == 0

    detail = status_json(git, docker, "jira-123")

    assert detail["state"] == "stopped"
    services = {s["name"]: s for s in detail["services"]}
    assert services["mysql"]["state"] == "missing"
    assert services["mysql"]["hostname"] == "mysql.jira-123.test"
    assert [s["ip"] for s in detail["services"]] == [None, None, None]
    rows = [
        line.split()
        for line in invoke(["jira-123", "status"], git, docker.compose, docker).stdout.splitlines()
    ]
    assert ["mysql", "missing", "-", "mysql.jira-123.test", "-"] in rows


def test_the_address_is_optional_in_the_status_model() -> None:
    service = ServiceStatus.model_validate(
        {"name": "mysql", "state": "missing", "health": None, "hostname": "mysql.a.test"}
    )

    assert service.ip is None


def test_a_missing_clone_is_reported(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "api")
    up("jira-123", git, docker, "api")
    clone = env.set_dir("jira-123") / "api"
    (clone / ".git").rmdir()
    clone.rmdir()

    detail = status_json(git, docker, "jira-123")

    assert detail["repos"][0]["exists"] is False
    assert detail["repos"][0]["sync"] == "no-clone"
    rows = invoke(["jira-123", "status"], git, docker.compose, docker).stdout
    assert "clone missing" in rows


def test_set_status_brings_the_agents_commits_into_the_repo(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    api = make_repo(env, git, "api")
    up("jira-123", git, docker, "api")
    git.clone(env.set_dir("jira-123") / "api").commits.append("agent work")

    detail = status_json(git, docker, "jira-123")

    assert detail["repos"][0]["sync"] == "updated"
    assert git.repo(api).commits["agent/jira-123"] == ["agent work"]


def test_set_status_names_a_repo_whose_agent_branch_could_not_be_synced(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "api")
    up("jira-123", git, docker, "api")
    git.clone(env.set_dir("jira-123") / "api").sync_result = SyncResult(
        SyncOutcome.DIVERGED, "agent/jira-123 has commits the agent's branch doesn't"
    )

    table = invoke(["jira-123", "status"], git, docker.compose, docker).stdout

    assert "api: agent branch not synced into api's repo (diverged)" in table
    assert "has commits the agent's branch doesn't" in table


def test_listing_every_set_syncs_nothing(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "api")
    up("jira-123", git, docker, "api")
    git.syncs.clear()

    assert invoke(["status"], git, docker.compose, docker).exit_code == 0

    assert git.syncs == []


def test_set_status_of_an_unknown_set_fails(env: Env) -> None:
    result = invoke(["nope", "status"], FakeGit(), FakeCompose(), FakeDocker())

    assert result.exit_code == 1
    assert "no set 'nope'" in result.stderr
