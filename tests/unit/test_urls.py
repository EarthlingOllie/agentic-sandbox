"""`sandbox SET urls [--json]`: every host-resolvable address of a set, with its IP."""

import json
from typing import Any

from sandbox.compose import project_name
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_certs import FakeMkcert
from tests.fakes_compose import FakeCompose
from tests.fakes_docker import FakeDocker
from tests.unit.test_web import make_repo, mkcert

__all__ = ["mkcert"]  # the fixture, re-exported for pytest

APP = """\
services:
  web:
    image: node:22-alpine
    ports:
      - "8000:8000"
    x-sandbox:
      web: true
      hosts: [admin]
      vite: 5173
  mysql:
    image: mysql:8.4
    ports:
      - "3306:3306"
      - "33060:33060"
  redis:
    image: redis:7
"""


def up(git: FakeGit, docker: FakeDocker, mkcert: FakeMkcert, *repos: str) -> None:
    result = invoke(["jira-123", "up", "--repos", *repos], git, docker.compose, docker, mkcert)
    assert result.exit_code == 0, result.output


def urls(git: FakeGit, docker: FakeDocker, *args: str, set_name: str = "jira-123") -> Any:  # noqa: ANN401 - JSON or text
    result = invoke([set_name, "urls", *args], git, docker.compose, docker)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout) if "--json" in args else result.stdout


def test_lists_web_apps_as_https_urls_and_services_as_host_port_with_ips(
    env: Env, mkcert: FakeMkcert
) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "web", APP)
    up(git, docker, mkcert, "web")

    listing = urls(git, docker, "--json")

    # Leases are handed out in hostname order: mysql .1, redis .2, web .3.
    assert listing == {
        "name": "jira-123",
        "state": "running",
        "running": True,
        "web": [
            {"url": "https://web.jira-123.test", "service": "web", "ip": "127.42.0.3"},
            {"url": "https://admin.jira-123.test", "service": "web", "ip": "127.42.0.3"},
            {"url": "https://web.jira-123.test:5173", "service": "web", "ip": "127.42.0.3"},
        ],
        "services": [
            {"address": "mysql.jira-123.test:3306", "service": "mysql", "ip": "127.42.0.1"},
            {"address": "mysql.jira-123.test:33060", "service": "mysql", "ip": "127.42.0.1"},
            {"address": "redis.jira-123.test", "service": "redis", "ip": "127.42.0.2"},
        ],
    }


def test_the_table_lists_the_same_addresses_as_the_json(env: Env, mkcert: FakeMkcert) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "web", APP)
    up(git, docker, mkcert, "web")

    table = [line.split() for line in urls(git, docker).splitlines()]

    assert table == [
        ["set", "jira-123:", "running"],
        [],
        ["WEB", "IP"],
        ["https://web.jira-123.test", "127.42.0.3"],
        ["https://admin.jira-123.test", "127.42.0.3"],
        ["https://web.jira-123.test:5173", "127.42.0.3"],
        [],
        ["SERVICE", "IP"],
        ["mysql.jira-123.test:3306", "127.42.0.1"],
        ["mysql.jira-123.test:33060", "127.42.0.1"],
        ["redis.jira-123.test", "127.42.0.2"],
    ]


def test_a_stopped_set_lists_its_sticky_leases_marked_not_running(
    env: Env, mkcert: FakeMkcert
) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "web", APP)
    up(git, docker, mkcert, "web")
    running = urls(git, docker, "--json")
    assert invoke(["jira-123", "down"], git, docker.compose, docker).exit_code == 0

    stopped = urls(git, docker, "--json")

    assert stopped["state"] == "stopped"
    assert stopped["running"] is False
    assert stopped["web"] == running["web"]
    assert stopped["services"] == running["services"]
    assert "not running" in urls(git, docker).splitlines()[0]


def test_a_partly_running_set_is_marked_not_running(env: Env, mkcert: FakeMkcert) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git, "web", APP)
    up(git, docker, mkcert, "web")
    docker.compose.stopped_services[project_name("jira-123")] = {"redis"}

    listing = urls(git, docker, "--json")

    assert (listing["state"], listing["running"]) == ("partial", False)


def test_a_set_without_services_has_no_addresses(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    env.make_repo_dir("docs")
    git.add_repo(env.cwd / "docs")
    assert invoke(["jira-123", "up", "--repos", "docs"], git, docker.compose, docker).exit_code == 0

    listing = urls(git, docker, "--json")

    assert (listing["web"], listing["services"]) == ([], [])
    assert "no web apps or services" in urls(git, docker)


def test_an_unknown_set_fails(env: Env) -> None:
    result = invoke(["nope", "urls"], FakeGit(), FakeCompose(), FakeDocker())

    assert result.exit_code == 1
    assert "no set 'nope'" in result.output


def test_urls_needs_a_set(env: Env) -> None:
    result = invoke(["urls"], FakeGit(), FakeCompose(), FakeDocker())

    assert result.exit_code == 2
    assert "urls needs a set" in result.output
