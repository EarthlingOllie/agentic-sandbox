"""No verb leases an IP or touches a hosts block: a set's addressing state is Docker's alone.

The loopback model kept a lease file under the state directory and a `# sandbox:<set>` block
in the hosts file. Both may still be on a machine that ran it; nothing reads, writes or
removes them any more.
"""

import json
from pathlib import Path

import pytest

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_docker import FakeDocker

MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n    ports:\n      - '3306:3306'\n"
LEFTOVER_HOSTS = (
    "127.0.0.1\tlocalhost\n"
    "# sandbox:jira-123\n"
    "127.42.0.1\tmysql.jira-123.test\n"
    "# end sandbox:jira-123\n"
)
LEFTOVER_LEASES = json.dumps(
    {
        "pool": "127.42.0.0/16",
        "sets": {"jira-123": {"block": 0, "ips": {"mysql.jira-123.test": "127.42.0.1"}}},
    }
)


def make_repo(env: Env, git: FakeGit) -> Path:
    path = env.make_repo_dir("api")
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(MYSQL)
    return path


def run(args: list[str], git: FakeGit, docker: FakeDocker) -> None:
    result = invoke(args, git, docker.compose, docker)
    assert result.exit_code == 0, result.output


def host_state(env: Env) -> dict[str, str]:
    """Everything under the state directory except locks, plus the hosts file."""
    found = {
        str(path.relative_to(env.state_dir)): path.read_text()
        for path in env.state_dir.rglob("*")
        if path.is_file() and path.suffix != ".lock"
    }
    if env.hosts_file.exists():
        found["hosts"] = env.hosts_file.read_text()
    return found


def test_the_whole_life_of_a_set_writes_no_lease_and_no_hosts_block(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)

    seen: list[dict[str, str]] = []
    for args in (
        ["jira-123", "up", "--repos", "api"],
        ["jira-123", "up", "--recreate"],
        ["jira-123", "down"],
        ["jira-123", "up"],
        ["jira-123", "down"],
        ["jira-123", "prune"],
        ["prune"],
        ["jira-123", "destroy"],
    ):
        run(args, git, docker)
        seen.append(host_state(env))

    assert seen == [{}] * 8


def test_what_the_loopback_model_left_behind_is_neither_changed_nor_removed(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    env.state_dir.mkdir(parents=True, exist_ok=True)
    (env.state_dir / "leases.json").write_text(LEFTOVER_LEASES)
    env.hosts_file.write_text(LEFTOVER_HOSTS)
    before = host_state(env)

    for args in (
        ["jira-123", "up", "--repos", "api"],
        ["jira-123", "down"],
        ["jira-123", "prune"],
        ["prune"],
        ["jira-123", "destroy"],
    ):
        run(args, git, docker)
        assert host_state(env) == before, args


def test_a_rolled_back_up_writes_no_lease_and_no_hosts_block(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    docker.compose.up_error = "service mysql is unhealthy"

    result = invoke(["jira-123", "up", "--repos", "api"], git, docker.compose, docker)

    assert result.exit_code == 1
    assert "unhealthy" in result.output
    assert host_state(env) == {}


def test_a_failed_validation_leaves_no_trace_of_the_set(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    api = make_repo(env, git)
    # No service named postgres: the init-hook check fails before anything is created.
    (api / ".sandbox" / "init").mkdir()
    (api / ".sandbox" / "init" / "postgres.sh").write_text("echo hi\n")

    result = invoke(["jira-123", "up", "--repos", "api"], git, docker.compose, docker)

    assert result.exit_code == 1
    assert "postgres" in result.stderr
    assert host_state(env) == {}
    assert not env.set_dir("jira-123").exists()
    listing = invoke(["status", "--json"], git, docker.compose, docker)
    assert json.loads(listing.stdout) == {"sets": []}


@pytest.mark.parametrize("verb", ["up", "down", "prune", "destroy"])
def test_no_verb_needs_the_hosts_file_to_be_writable(env: Env, verb: str) -> None:
    """The configured hosts file is a directory here: any attempt to write it would fail."""
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    env.hosts_file.mkdir()
    steps = {
        "up": [["jira-123", "up", "--repos", "api"]],
        "down": [["jira-123", "up", "--repos", "api"], ["jira-123", "down"]],
        "prune": [
            ["jira-123", "up", "--repos", "api"],
            ["jira-123", "down"],
            ["jira-123", "prune"],
        ],
        "destroy": [
            ["jira-123", "up", "--repos", "api"],
            ["jira-123", "down"],
            ["jira-123", "destroy"],
        ],
    }[verb]

    for args in steps:
        run(args, git, docker)
