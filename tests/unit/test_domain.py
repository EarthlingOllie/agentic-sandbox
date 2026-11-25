"""The configured ``domain``: every set hostname ends in it (``<name>.<set>.<domain>``).

The default (``test``) is covered by every other module; this one runs the same CLI seam
with a multi-label domain in the config.
"""

import json
from typing import Any

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_certs import FIXTURES, FakeMkcert
from tests.fakes_compose import FakeCompose, load_compose_yaml
from tests.fakes_docker import FakeDocker
from tests.unit.test_web import make_repo, mkcert

__all__ = ["mkcert"]  # the fixture, re-exported for pytest

DOMAIN = "sandbox.test"

MYSQL_AND_REDIS = """\
services:
  mysql:
    image: mysql:8.4
    ports:
      - "3306:3306"
  redis:
    image: redis:7
"""

APP = """\
services:
  web:
    image: node:22-alpine
    x-sandbox:
      web: true
      hosts: [admin]
      vite: 5173
  mysql:
    image: mysql:8.4
    ports:
      - "3306:3306"
"""


def set_domain(env: Env, domain: str = DOMAIN) -> None:
    with env.config_file.open("a") as config:
        config.write(f"domain: {domain}\n")


def services(env: Env, set_name: str = "jira-123") -> dict[str, Any]:
    return load_compose_yaml(env.set_dir(set_name) / "override.yaml")["services"]


def test_services_answer_to_set_hostnames_under_the_configured_domain(env: Env) -> None:
    set_domain(env)
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    result = invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose())

    assert result.exit_code == 0, result.output
    # On the set network: the short name and the set hostname.
    assert services(env)["mysql"]["networks"] == {
        "default": {"aliases": ["mysql", "mysql.jira-123.sandbox.test"]}
    }
    assert services(env)["redis"]["networks"] == {
        "default": {"aliases": ["redis", "redis.jira-123.sandbox.test"]}
    }
    # From the machine: the printed hostnames, on their conventional ports.
    assert "  mysql.jira-123.sandbox.test  3306/tcp\n" in result.stdout
    assert "  redis.jira-123.sandbox.test  (no ports)\n" in result.stdout


def test_a_web_app_is_served_under_the_configured_domain(env: Env, mkcert: FakeMkcert) -> None:
    set_domain(env)
    git = FakeGit()
    make_repo(env, git, "web", APP)

    result = invoke(["jira-123", "up", "--repos", "web"], git, FakeCompose(), mkcert=mkcert)

    assert result.exit_code == 0, result.output
    web = services(env)["web"]
    # Its hostname and its extra host are names on the set network...
    assert web["networks"]["default"]["aliases"] == [
        "web",
        "web.jira-123.sandbox.test",
        "admin.jira-123.sandbox.test",
    ]
    # ...and are printed, each answering on HTTPS and the Vite port.
    assert "  web.jira-123.sandbox.test  443/tcp, 5173/tcp\n" in result.stdout
    assert "  admin.jira-123.sandbox.test  443/tcp, 5173/tcp\n" in result.stdout
    # The app is told its own URL and HMR host.
    assert web["environment"] == {
        "WORKTREE": str(env.set_dir("jira-123") / "web"),
        "APP_URL": "https://web.jira-123.sandbox.test",
        "VITE_HMR_HOST": "web.jira-123.sandbox.test",
    }
    # The set's wildcard certificate is issued for the domain.
    tls = env.set_dir("jira-123") / "tls"
    assert mkcert.issued == [(tls / "cert.pem", tls / "key.pem", ["*.jira-123.sandbox.test"])]


def test_a_certificate_issued_for_another_domain_is_reissued_on_the_next_up(
    env: Env, mkcert: FakeMkcert
) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", APP)
    first = invoke(["jira-123", "up", "--repos", "web"], git, compose, mkcert=mkcert)
    assert first.exit_code == 0, first.output
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0
    tls = env.set_dir("jira-123") / "tls"
    assert (tls / "cert.pem").read_bytes() == (FIXTURES / "jira-123.pem").read_bytes()

    set_domain(env)
    again = invoke(["jira-123", "up"], git, compose, mkcert=mkcert)

    assert again.exit_code == 0, again.output
    assert [names for _, _, names in mkcert.issued] == [
        ["*.jira-123.test"],
        ["*.jira-123.sandbox.test"],
    ]
    assert (tls / "cert.pem").read_bytes() == (FIXTURES / "jira-123-sandbox-test.pem").read_bytes()
    # A further `up` under the same domain keeps the new certificate.
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0
    assert invoke(["jira-123", "up"], git, compose, mkcert=mkcert).exit_code == 0
    assert len(mkcert.issued) == 2


def test_status_shows_hostnames_under_the_configured_domain(env: Env) -> None:
    set_domain(env)
    git, docker = FakeGit(), FakeDocker()
    docker.addresses.update(
        {("jira-123", "mysql"): "172.20.0.2", ("jira-123", "redis"): "172.20.0.3"}
    )
    make_repo(env, git, "api", MYSQL_AND_REDIS)
    assert invoke(["jira-123", "up", "--repos", "api"], git, docker.compose, docker).exit_code == 0

    as_json = invoke(["jira-123", "status", "--json"], git, docker.compose, docker)
    table = invoke(["jira-123", "status"], git, docker.compose, docker)

    assert as_json.exit_code == 0, as_json.output
    hostnames = {
        s["name"]: (s["hostname"], s["ip"]) for s in json.loads(as_json.stdout)["services"]
    }
    assert hostnames == {
        "agent": ("agent.jira-123.sandbox.test", None),
        "mysql": ("mysql.jira-123.sandbox.test", "172.20.0.2"),
        "redis": ("redis.jira-123.sandbox.test", "172.20.0.3"),
    }
    assert table.exit_code == 0, table.output
    rows = [line.split() for line in table.stdout.splitlines()]
    assert ["mysql", "running", "-", "mysql.jira-123.sandbox.test", "172.20.0.2"] in rows
    assert ["redis", "running", "-", "redis.jira-123.sandbox.test", "172.20.0.3"] in rows


def test_urls_lists_addresses_under_the_configured_domain(env: Env, mkcert: FakeMkcert) -> None:
    set_domain(env)
    git, docker = FakeGit(), FakeDocker()
    docker.addresses.update(
        {("jira-123", "mysql"): "172.20.0.2", ("jira-123", "web"): "172.20.0.3"}
    )
    make_repo(env, git, "web", APP)
    up = invoke(["jira-123", "up", "--repos", "web"], git, docker.compose, docker, mkcert)
    assert up.exit_code == 0, up.output

    as_json = invoke(["jira-123", "urls", "--json"], git, docker.compose, docker)
    table = invoke(["jira-123", "urls"], git, docker.compose, docker)

    assert as_json.exit_code == 0, as_json.output
    listing = json.loads(as_json.stdout)
    assert listing["web"] == [
        {"url": "https://web.jira-123.sandbox.test", "service": "web", "ip": "172.20.0.3"},
        {"url": "https://admin.jira-123.sandbox.test", "service": "web", "ip": "172.20.0.3"},
        {"url": "https://web.jira-123.sandbox.test:5173", "service": "web", "ip": "172.20.0.3"},
    ]
    assert listing["services"] == [
        {"address": "mysql.jira-123.sandbox.test:3306", "service": "mysql", "ip": "172.20.0.2"},
    ]
    assert table.exit_code == 0, table.output
    rows = [line.split() for line in table.stdout.splitlines()]
    assert ["https://web.jira-123.sandbox.test", "172.20.0.3"] in rows
    assert ["https://admin.jira-123.sandbox.test", "172.20.0.3"] in rows
    assert ["https://web.jira-123.sandbox.test:5173", "172.20.0.3"] in rows
    assert ["mysql.jira-123.sandbox.test:3306", "172.20.0.2"] in rows
