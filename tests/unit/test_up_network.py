"""`up` publishes nothing: services answer on their conventional ports at their own address."""

from pathlib import Path
from typing import Any

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose, load_compose_yaml

MYSQL = """\
services:
  mysql:
    image: mysql:8.4
    ports:
      - "3306:3306"
"""

MYSQL_AND_REDIS = """\
services:
  mysql:
    image: mysql:8.4
    ports:
      - "127.0.0.1:13306:3306"
  redis:
    image: redis:7
    ports:
      - target: 6379
        published: "16379"
      - "6380/udp"
    networks: [back]
  worker:
    image: busybox
networks:
  back: {}
"""

EVERY_BINDING_FORM = """\
services:
  mysql:
    image: mysql:8.4
    ports:
      - "0.0.0.0:3306:3306"
      - "127.0.0.1:13306:3306"
      - "33060:33060"
      - "8080"
      - target: 9104
        published: "9104"
        host_ip: 0.0.0.0
"""


def make_repo(env: Env, git: FakeGit, name: str, compose: str) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def override(env: Env, set_name: str) -> dict[str, Any]:
    return load_compose_yaml(env.set_dir(set_name) / "override.yaml")


def test_no_service_publishes_a_port(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    result = invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose())

    assert result.exit_code == 0, result.output
    services = override(env, "jira-123")["services"]
    assert {name: spec.get("ports") for name, spec in services.items() if name != "agent"} == {
        "mysql": [],
        "redis": [],
        "worker": [],
    }
    assert "ports" not in services["agent"]


def test_every_host_side_binding_a_repo_declares_is_reset(env: Env) -> None:
    """Compose would otherwise append to the repo's own bindings, `0.0.0.0` ones included."""
    git = FakeGit()
    make_repo(env, git, "api", EVERY_BINDING_FORM)

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    text = (env.set_dir("jira-123") / "override.yaml").read_text()
    # `!override []` replaces the repo's list; a plain empty list would be merged into it.
    assert "    ports: !override []\n" in text
    # No port number and no host address survives (`0.0.0.0`, `127.0.0.1`).
    assert "3306" not in text
    assert "0.0" not in text


def test_each_service_is_aliased_by_short_name_and_set_hostname(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    services = override(env, "jira-123")["services"]
    # The short name keeps services reaching each other inside the set.
    assert services["mysql"]["networks"] == {
        "default": {"aliases": ["mysql", "mysql.jira-123.test"]}
    }
    # A service on its own networks keeps them and joins the set network too.
    assert services["redis"]["networks"] == {
        "back": None,
        "default": {"aliases": ["redis", "redis.jira-123.test"]},
    }


def test_two_sets_with_the_same_service_get_the_same_override_but_for_their_names(
    env: Env,
) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL)

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0
    assert invoke(["jira-456", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    first = override(env, "jira-123")["services"]["mysql"]
    second = override(env, "jira-456")["services"]["mysql"]
    assert first["ports"] == second["ports"] == []
    assert first["networks"]["default"]["aliases"] == ["mysql", "mysql.jira-123.test"]
    assert second["networks"]["default"]["aliases"] == ["mysql", "mysql.jira-456.test"]


def test_up_prints_each_set_hostname_with_its_conventional_ports(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    result = invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose())

    assert result.exit_code == 0, result.output
    assert "  hostnames:\n" in result.stdout
    assert "    mysql.jira-123.test  3306/tcp\n" in result.stdout
    assert "    redis.jira-123.test  6379/tcp, 6380/udp\n" in result.stdout
    assert "    worker.jira-123.test  (no ports)\n" in result.stdout
    # Neither a loopback address nor the repo's own host-side port numbers.
    assert "127." not in result.stdout
    assert "13306" not in result.stdout
    assert "16379" not in result.stdout


def test_up_prints_the_hostnames_on_the_no_op_path_too(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL)
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0

    result = invoke(["jira-123", "up"], git, compose)

    assert result.exit_code == 0, result.output
    assert "already running" in result.stdout
    assert "    mysql.jira-123.test  3306/tcp\n" in result.stdout


def test_a_set_without_services_prints_no_hostnames(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", "services: {}\n")

    result = invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose())

    assert result.exit_code == 0, result.output
    assert "hostnames" not in result.stdout


def test_a_service_outside_the_set_network_loses_its_bindings_too(env: Env) -> None:
    git = FakeGit()
    make_repo(
        env,
        git,
        "api",
        "services:\n  probe:\n    image: busybox\n    network_mode: none\n"
        '    ports:\n      - "8080:80"\n',
    )

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    # No network to alias it on, but the host-side binding is reset all the same.
    assert override(env, "jira-123")["services"]["probe"] == {
        "labels": {"sandbox.set": "jira-123"},
        "ports": [],
    }
