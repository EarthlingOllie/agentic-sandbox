"""`sandbox SET urls [--json]`: every address of a set, with the container's current address."""

import json
from typing import Any

from sandbox.compose import project_name
from sandbox.models.urls import ServiceAddress, WebUrl
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


ADDRESSES = {
    ("jira-123", "mysql"): "172.20.0.2",
    ("jira-123", "redis"): "172.20.0.3",
    ("jira-123", "web"): "172.20.0.4",
}


def test_lists_web_apps_as_https_urls_and_services_as_host_port_on_conventional_ports(
    env: Env, mkcert: FakeMkcert
) -> None:
    git, docker = FakeGit(), FakeDocker(addresses=dict(ADDRESSES))
    make_repo(env, git, "web", APP)
    up(git, docker, mkcert, "web")

    listing = urls(git, docker, "--json")

    assert listing == {
        "name": "jira-123",
        "state": "running",
        "running": True,
        "web": [
            {"url": "https://web.jira-123.test", "service": "web", "ip": "172.20.0.4"},
            {"url": "https://admin.jira-123.test", "service": "web", "ip": "172.20.0.4"},
            {"url": "https://web.jira-123.test:5173", "service": "web", "ip": "172.20.0.4"},
        ],
        "services": [
            # Each declared container port; redis declares none, so it has no port.
            {"address": "mysql.jira-123.test:3306", "service": "mysql", "ip": "172.20.0.2"},
            {"address": "mysql.jira-123.test:33060", "service": "mysql", "ip": "172.20.0.2"},
            {"address": "redis.jira-123.test", "service": "redis", "ip": "172.20.0.3"},
        ],
    }


def test_the_container_port_is_listed_whatever_host_port_the_repo_binds(
    env: Env, mkcert: FakeMkcert
) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(
        env,
        git,
        "api",
        "services:\n  mysql:\n    image: mysql:8.4\n    ports:\n"
        '      - "0.0.0.0:13306:3306"\n      - "5353:53/udp"\n',
    )
    up(git, docker, mkcert, "api")

    listing = urls(git, docker, "--json")

    assert [entry["address"] for entry in listing["services"]] == [
        "mysql.jira-123.test:3306",
        "mysql.jira-123.test:53/udp",
    ]


def test_the_table_lists_the_same_addresses_as_the_json(env: Env, mkcert: FakeMkcert) -> None:
    git, docker = FakeGit(), FakeDocker(addresses=dict(ADDRESSES))
    make_repo(env, git, "web", APP)
    up(git, docker, mkcert, "web")

    table = [line.split() for line in urls(git, docker).splitlines()]

    assert table == [
        ["set", "jira-123:", "running"],
        [],
        ["WEB", "IP"],
        ["https://web.jira-123.test", "172.20.0.4"],
        ["https://admin.jira-123.test", "172.20.0.4"],
        ["https://web.jira-123.test:5173", "172.20.0.4"],
        [],
        ["SERVICE", "IP"],
        ["mysql.jira-123.test:3306", "172.20.0.2"],
        ["mysql.jira-123.test:33060", "172.20.0.2"],
        ["redis.jira-123.test", "172.20.0.3"],
    ]


def test_a_stopped_set_lists_the_same_addresses_without_ips(env: Env, mkcert: FakeMkcert) -> None:
    git, docker = FakeGit(), FakeDocker(addresses=dict(ADDRESSES))
    make_repo(env, git, "web", APP)
    up(git, docker, mkcert, "web")
    running = urls(git, docker, "--json")
    assert invoke(["jira-123", "down"], git, docker.compose, docker).exit_code == 0

    stopped = urls(git, docker, "--json")

    assert stopped["state"] == "stopped"
    assert stopped["running"] is False
    assert [w["url"] for w in stopped["web"]] == [w["url"] for w in running["web"]]
    assert [s["address"] for s in stopped["services"]] == [
        s["address"] for s in running["services"]
    ]
    assert {entry["ip"] for entry in [*stopped["web"], *stopped["services"]]} == {None}
    table = urls(git, docker)
    assert "not running" in table.splitlines()[0]
    assert "lease" not in table
    assert ["mysql.jira-123.test:3306", "-"] in [line.split() for line in table.splitlines()]


def test_the_address_is_optional_in_the_urls_model() -> None:
    web = WebUrl.model_validate({"url": "https://web.a.test", "service": "web"})
    service = ServiceAddress.model_validate({"address": "mysql.a.test:3306", "service": "mysql"})

    assert (web.ip, service.ip) == (None, None)


def test_a_partly_running_set_is_marked_not_running(env: Env, mkcert: FakeMkcert) -> None:
    git, docker = FakeGit(), FakeDocker(addresses=dict(ADDRESSES))
    make_repo(env, git, "web", APP)
    up(git, docker, mkcert, "web")
    docker.compose.stopped_services[project_name("jira-123")] = {"redis"}

    listing = urls(git, docker, "--json")

    assert (listing["state"], listing["running"]) == ("partial", False)
    # The services still running keep their addresses; the stopped one has none.
    assert {s["service"]: s["ip"] for s in listing["services"]} == {
        "mysql": "172.20.0.2",
        "redis": None,
    }


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
