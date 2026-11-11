"""`up` gives every hostname its own leased loopback IP, native ports and `.test` aliases."""

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


def make_repo(env: Env, git: FakeGit, name: str, compose: str) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def override(env: Env, set_name: str) -> dict[str, Any]:
    return load_compose_yaml(env.set_dir(set_name) / "override.yaml")


def test_each_port_is_published_natively_on_the_hostnames_own_ip(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    result = invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose())

    assert result.exit_code == 0, result.output
    services = override(env, "jira-123")["services"]
    # The repo's host-side port numbers are dropped: each hostname keeps the native port.
    assert services["mysql"]["ports"] == ["127.42.0.1:3306:3306/tcp"]
    assert services["redis"]["ports"] == ["127.42.0.2:6379:6379/tcp", "127.42.0.2:6380:6380/udp"]
    assert "ports" not in services["worker"]


def test_ports_replace_the_repos_own_bindings(env: Env) -> None:
    """Compose would otherwise append the override's ports to the repo's 0.0.0.0 ones."""
    git = FakeGit()
    make_repo(env, git, "api", MYSQL)

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    text = (env.set_dir("jira-123") / "override.yaml").read_text()
    assert "ports: !override\n    - 127.42.0.1:3306:3306/tcp\n" in text


def test_each_service_is_aliased_by_short_name_and_set_hostname(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    services = override(env, "jira-123")["services"]
    assert services["mysql"]["networks"] == {
        "default": {"aliases": ["mysql", "mysql.jira-123.test"]}
    }
    # A service on its own networks keeps them and joins the set network too.
    assert services["redis"]["networks"] == {
        "back": None,
        "default": {"aliases": ["redis", "redis.jira-123.test"]},
    }


def test_two_sets_with_the_same_service_get_different_ips(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL)

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0
    assert invoke(["jira-456", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    first = override(env, "jira-123")["services"]["mysql"]["ports"]
    second = override(env, "jira-456")["services"]["mysql"]["ports"]
    assert first == ["127.42.0.1:3306:3306/tcp"]
    assert second == ["127.42.1.1:3306:3306/tcp"]


def test_down_then_up_keeps_the_same_ips(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL)
    assert invoke(["other", "up", "--repos", "api"], git, compose).exit_code == 0
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0
    before = override(env, "jira-123")["services"]["mysql"]["ports"]

    assert invoke(["jira-123", "down"], git, compose).exit_code == 0
    assert invoke(["jira-123", "up"], git, compose).exit_code == 0

    assert override(env, "jira-123")["services"]["mysql"]["ports"] == before
    assert before == ["127.42.1.1:3306:3306/tcp"]


def test_the_pool_comes_from_the_config(env: Env) -> None:
    with env.config_file.open("a") as config:
        config.write("ip_pool: 127.50.0.0/16\n")
    git = FakeGit()
    make_repo(env, git, "api", MYSQL)

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    ports = override(env, "jira-123")["services"]["mysql"]["ports"]
    assert ports == ["127.50.0.1:3306:3306/tcp"]


def test_up_prints_each_binding(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    result = invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose())

    assert result.exit_code == 0, result.output
    assert "  mysql.jira-123.test  127.42.0.1  3306/tcp\n" in result.stdout
    assert "  redis.jira-123.test  127.42.0.2  6379/tcp, 6380/udp\n" in result.stdout
    assert "  worker.jira-123.test  127.42.0.3  (no ports)\n" in result.stdout


def test_up_prints_the_bindings_on_the_no_op_path_too(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL)
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0

    result = invoke(["jira-123", "up"], git, compose)

    assert result.exit_code == 0, result.output
    assert "already running" in result.stdout
    assert "mysql.jira-123.test  127.42.0.1  3306/tcp" in result.stdout


def test_a_set_without_services_leases_nothing(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", "services: {}\n")

    result = invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose())

    assert result.exit_code == 0, result.output
    assert "bindings" not in result.stdout
    assert not (env.state_dir / "leases.json").exists()
