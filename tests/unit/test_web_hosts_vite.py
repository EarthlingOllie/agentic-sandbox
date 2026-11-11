"""Extra web hostnames (`hosts: [admin, portal]`) and the Vite dev server (`vite: 5173`).

Checked through the CLI seam and the rendered override, with mkcert faked by real fixture
certificates (``tests.fakes_certs``).
"""

import pytest

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_certs import FakeMkcert
from tests.fakes_compose import FakeCompose
from tests.unit.test_web import make_repo, mkcert, services, up

__all__ = ["mkcert"]  # the fixture, re-exported for pytest

MULTI_HOST = """\
services:
  web:
    image: python:3.12-alpine
    x-sandbox:
      web: true
      hosts: [admin, portal]
  mysql:
    image: mysql:8.4
"""

VITE = """\
services:
  web:
    image: node:22-alpine
    x-sandbox:
      web: true
      vite: 5173
"""


def hosts_entries(env: Env) -> dict[str, str]:
    lines = env.hosts_file.read_text().splitlines()
    return {line.split("\t")[1]: line.split("\t")[0] for line in lines if "\t" in line}


def test_extra_hosts_share_the_web_apps_ip_in_the_hosts_block(env: Env, mkcert: FakeMkcert) -> None:
    git = FakeGit()
    make_repo(env, git, "web", MULTI_HOST)

    up(env, git, FakeCompose(), mkcert, "web")

    # mysql leases .1, the web app .2; its extra hosts route to the same IP.
    assert hosts_entries(env) == {
        "mysql.jira-123.test": "127.42.0.1",
        "web.jira-123.test": "127.42.0.2",
        "admin.jira-123.test": "127.42.0.2",
        "portal.jira-123.test": "127.42.0.2",
    }


def test_extra_hosts_are_network_aliases_of_the_web_service(env: Env, mkcert: FakeMkcert) -> None:
    git = FakeGit()
    make_repo(env, git, "web", MULTI_HOST)

    up(env, git, FakeCompose(), mkcert, "web")

    assert services(env)["web"]["networks"]["default"]["aliases"] == [
        "web",
        "web.jira-123.test",
        "admin.jira-123.test",
        "portal.jira-123.test",
    ]
    assert services(env)["mysql"]["networks"]["default"]["aliases"] == [
        "mysql",
        "mysql.jira-123.test",
    ]


def test_extra_hosts_are_covered_by_the_sets_wildcard_certificate(
    env: Env, mkcert: FakeMkcert
) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", MULTI_HOST)

    up(env, git, compose, mkcert, "web")
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0
    up(env, git, compose, mkcert)

    # One `*.jira-123.test` leaf, verified for every hostname and reused while valid.
    tls = env.set_dir("jira-123") / "tls"
    assert mkcert.issued == [(tls / "cert.pem", tls / "key.pem", ["*.jira-123.test"])]


def test_extra_hosts_are_printed_with_the_web_apps_bindings(env: Env, mkcert: FakeMkcert) -> None:
    git = FakeGit()
    make_repo(env, git, "web", MULTI_HOST)

    output = up(env, git, FakeCompose(), mkcert, "web")

    assert "  admin.jira-123.test  127.42.0.2  " in output
    assert "  portal.jira-123.test  127.42.0.2  " in output


@pytest.mark.parametrize(
    ("hosts", "message"),
    [
        ("[admin.eu]", "extra host 'admin.eu' must be a single DNS label"),
        ("[Admin]", "extra host 'Admin' must be a single DNS label"),
        ("[-admin]", "extra host '-admin' must be a single DNS label"),
        ('[""]', "extra host '' must be a single DNS label"),
        ("admin", "x-sandbox.hosts: expected a list of host names"),
        ("[1]", "x-sandbox.hosts: expected a list of host names"),
    ],
)
def test_malformed_extra_hosts_fail_before_any_container_starts(
    env: Env, mkcert: FakeMkcert, hosts: str, message: str
) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", MULTI_HOST.replace("[admin, portal]", hosts))

    result = invoke(["jira-123", "up", "--repos", "web"], git, compose, mkcert=mkcert)

    assert result.exit_code == 1
    assert message in result.output
    assert compose.calls == []
    assert mkcert.issued == []


@pytest.mark.parametrize(
    ("hosts", "other_repo", "message"),
    [
        (
            "[mysql]",
            None,
            "hostname mysql.jira-123.test is used more than once in set 'jira-123': "
            "extra host of the web app of repo web; service 'mysql' of repo web",
        ),
        (
            "[admin, admin]",
            None,
            "hostname admin.jira-123.test is used more than once in set 'jira-123': "
            "extra host of the web app of repo web; extra host of the web app of repo web",
        ),
        (
            "[web]",
            None,
            "hostname web.jira-123.test is used more than once in set 'jira-123': "
            "web app of repo web; extra host of the web app of repo web",
        ),
        (
            "[admin]",
            "services:\n  admin:\n    image: nginx\n    x-sandbox: {web: true}\n",
            "hostname admin.jira-123.test is used more than once in set 'jira-123': "
            "web app of repo admin; extra host of the web app of repo web",
        ),
        (
            "[cache]",
            "services:\n  cache:\n    image: redis:7\n",
            "hostname cache.jira-123.test is used more than once in set 'jira-123': "
            "extra host of the web app of repo web; service 'cache' of repo admin",
        ),
    ],
)
def test_extra_hosts_take_part_in_the_duplicate_hostname_check(
    env: Env, mkcert: FakeMkcert, hosts: str, other_repo: str | None, message: str
) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", MULTI_HOST.replace("[admin, portal]", hosts))
    repos = ["web"]
    if other_repo is not None:
        make_repo(env, git, "admin", other_repo)
        repos.append("admin")

    result = invoke(["jira-123", "up", "--repos", *repos], git, compose, mkcert=mkcert)

    assert result.exit_code == 1
    assert message in result.output
    assert compose.calls == []


def test_vite_port_is_published_on_the_apps_ip_with_the_hmr_host(
    env: Env, mkcert: FakeMkcert
) -> None:
    git = FakeGit()
    make_repo(env, git, "web", VITE)

    up(env, git, FakeCompose(), mkcert, "web")

    web = services(env)["web"]
    assert web["ports"] == ["127.42.0.1:443:443/tcp", "127.42.0.1:5173:5173/tcp"]
    assert web["environment"] == {
        "WORKTREE": str(env.set_dir("jira-123") / "web"),
        "APP_URL": "https://web.jira-123.test",
        "VITE_HMR_HOST": "web.jira-123.test",
    }


def test_a_declared_vite_port_is_not_published_twice(env: Env, mkcert: FakeMkcert) -> None:
    git = FakeGit()
    make_repo(
        env, git, "web", VITE.replace("    x-sandbox:", '    ports: ["5173:5173"]\n    x-sandbox:')
    )

    up(env, git, FakeCompose(), mkcert, "web")

    assert services(env)["web"]["ports"] == ["127.42.0.1:5173:5173/tcp", "127.42.0.1:443:443/tcp"]


def test_without_vite_no_hmr_host_is_set(env: Env, mkcert: FakeMkcert) -> None:
    git = FakeGit()
    make_repo(env, git, "web", MULTI_HOST)

    up(env, git, FakeCompose(), mkcert, "web")

    assert "VITE_HMR_HOST" not in services(env)["web"]["environment"]
    assert services(env)["web"]["ports"] == ["127.42.0.2:443:443/tcp"]


@pytest.mark.parametrize("vite", ["true", "0", "70000", "443", '"5173"'])
def test_an_invalid_vite_port_fails_before_any_container_starts(
    env: Env, mkcert: FakeMkcert, vite: str
) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", VITE.replace("vite: 5173", f"vite: {vite}"))

    result = invoke(["jira-123", "up", "--repos", "web"], git, compose, mkcert=mkcert)

    assert result.exit_code == 1
    assert "x-sandbox.vite: expected a port number other than 443" in result.output
    assert compose.calls == []
