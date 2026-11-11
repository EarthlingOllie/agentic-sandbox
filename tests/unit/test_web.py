"""HTTPS web apps: `x-sandbox: {web: true}` serves a repo's service at https://<repo>.<set>.test.

Checked through the CLI seam and the rendered override, with mkcert faked by real fixture
certificates (``tests.fakes_certs``).
"""

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_certs import FIXTURES, FakeMkcert
from tests.fakes_compose import FakeCompose, load_compose_yaml

WEB = """\
services:
  web:
    image: python:3.12-alpine
    environment:
      DEBUG: "1"
    x-sandbox:
      web: true
"""

WEB_AND_MYSQL = """\
services:
  web:
    image: python:3.12-alpine
    ports:
      - "8000:8000"
    x-sandbox: {web: true}
  mysql:
    image: mysql:8.4
"""

CA_TARGET = "/sandbox/ca/rootCA.pem"


def make_repo(env: Env, git: FakeGit, name: str, compose: str) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def services(env: Env, set_name: str = "jira-123") -> dict[str, Any]:
    return load_compose_yaml(env.set_dir(set_name) / "override.yaml")["services"]


def mounts(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {volume["target"]: volume for volume in service.get("volumes", [])}


@pytest.fixture
def mkcert(env: Env) -> FakeMkcert:
    fake = FakeMkcert(env.root / "mkcert")
    # The CA key sits beside rootCA.pem, as in a real CAROOT; it must never be mounted.
    (fake.ca_dir / "rootCA-key.pem").write_text("not for containers\n")
    return fake


def up(env: Env, git: FakeGit, compose: FakeCompose, mkcert: FakeMkcert, *repos: str) -> str:
    result = invoke(
        ["jira-123", "up", *(["--repos", *repos] if repos else [])], git, compose, mkcert=mkcert
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_web_app_publishes_443_on_its_own_ip_and_listens_unprivileged(
    env: Env, mkcert: FakeMkcert
) -> None:
    git = FakeGit()
    make_repo(env, git, "web", WEB_AND_MYSQL)

    up(env, git, FakeCompose(), mkcert, "web")

    web = services(env)["web"]
    # web.jira-123.test is the service's set hostname with its own leased IP (mysql gets .1).
    assert web["ports"] == ["127.42.0.2:8000:8000/tcp", "127.42.0.2:443:443/tcp"]
    assert "web.jira-123.test" in web["networks"]["default"]["aliases"]
    assert web["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": 0}
    # Only web apps listen on privileged ports.
    assert "sysctls" not in services(env)["mysql"]
    assert "web.jira-123.test" in env.hosts_file.read_text()


def test_a_declared_443_is_not_published_twice(env: Env, mkcert: FakeMkcert) -> None:
    git = FakeGit()
    make_repo(
        env, git, "web", WEB.replace("    x-sandbox:", '    ports: ["443:443"]\n    x-sandbox:')
    )

    up(env, git, FakeCompose(), mkcert, "web")

    assert services(env)["web"]["ports"] == ["127.42.0.1:443:443/tcp"]


def test_only_worktree_and_app_url_are_injected(env: Env, mkcert: FakeMkcert) -> None:
    git = FakeGit()
    make_repo(env, git, "web", WEB)

    up(env, git, FakeCompose(), mkcert, "web")

    # The repo's own environment stays in its file; the override adds exactly these two.
    assert services(env)["web"]["environment"] == {
        "WORKTREE": str(env.set_dir("jira-123") / "web"),
        "APP_URL": "https://web.jira-123.test",
    }


def test_a_relocated_web_app_keeps_its_own_environment(env: Env, mkcert: FakeMkcert) -> None:
    """A web app outside the project-directory repo is restated with `!override`."""
    git = FakeGit()
    make_repo(env, git, "api", "services:\n  mysql:\n    image: mysql:8.4\n")
    make_repo(env, git, "web", WEB)

    up(env, git, FakeCompose(), mkcert, "api", "web")

    text = (env.set_dir("jira-123") / "override.yaml").read_text()
    assert "environment: !override" in text
    assert services(env)["web"]["environment"] == {
        "DEBUG": "1",
        "WORKTREE": str(env.set_dir("jira-123") / "web"),
        "APP_URL": "https://web.jira-123.test",
    }


def test_the_leaf_certificate_is_issued_into_the_set_dir_and_mounted(
    env: Env, mkcert: FakeMkcert
) -> None:
    git = FakeGit()
    make_repo(env, git, "web", WEB)

    up(env, git, FakeCompose(), mkcert, "web")

    tls = env.set_dir("jira-123") / "tls"
    assert mkcert.issued == [(tls / "cert.pem", tls / "key.pem", ["*.jira-123.test"])]
    assert (tls / "cert.pem").is_file()
    # Readable by whatever user the web app's image runs as.
    assert (tls / "key.pem").stat().st_mode & 0o777 == 0o644
    assert mounts(services(env)["web"])["/sandbox/tls"] == {
        "type": "bind",
        "source": str(tls),
        "target": "/sandbox/tls",
        "read_only": True,
    }


def test_a_valid_certificate_is_reused_on_a_later_up(env: Env, mkcert: FakeMkcert) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", WEB)
    up(env, git, compose, mkcert, "web")
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0

    up(env, git, compose, mkcert)

    assert len(mkcert.issued) == 1


@pytest.mark.parametrize(
    "leaf",
    [
        "jira-123-other-ca",  # signed by a CA that is no longer the machine's
        "jira-123-short",  # expired, or expiring within the reissue margin
        None,  # not a certificate at all
    ],
)
def test_an_invalid_certificate_is_reissued(env: Env, mkcert: FakeMkcert, leaf: str | None) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", WEB)
    up(env, git, compose, mkcert, "web")
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0
    tls = env.set_dir("jira-123") / "tls"
    if leaf is None:
        (tls / "cert.pem").write_text("garbage\n")
    else:
        shutil.copyfile(FIXTURES / f"{leaf}.pem", tls / "cert.pem")
        shutil.copyfile(FIXTURES / f"{leaf}-key.pem", tls / "key.pem")

    up(env, git, compose, mkcert)

    assert len(mkcert.issued) == 2
    assert (tls / "cert.pem").read_bytes() == (FIXTURES / "jira-123.pem").read_bytes()


def test_a_certificate_that_does_not_verify_after_issuing_fails_up(
    env: Env, mkcert: FakeMkcert
) -> None:
    """E.g. $CAROOT names a different CA than the one mkcert signed with."""
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", WEB)
    mkcert.leaf = "jira-123-other-ca"

    result = invoke(["jira-123", "up", "--repos", "web"], git, compose, mkcert=mkcert)

    assert result.exit_code == 1
    assert "does not verify against" in result.output
    assert compose.calls == []


def test_only_root_ca_pem_is_mounted_into_every_container(env: Env, mkcert: FakeMkcert) -> None:
    git = FakeGit()
    make_repo(env, git, "web", WEB_AND_MYSQL)

    up(env, git, FakeCompose(), mkcert, "web")

    rendered = services(env)
    for name in ("agent", "mysql", "web"):
        assert mounts(rendered[name])[CA_TARGET] == {
            "type": "bind",
            "source": str(mkcert.ca_dir / "rootCA.pem"),
            "target": CA_TARGET,
            "read_only": True,
        }, name
    text = (env.set_dir("jira-123") / "override.yaml").read_text()
    assert "rootCA-key.pem" not in text
    # Node (claude) trusts it too; the bundled image adds it to the system store.
    assert rendered["agent"]["environment"]["NODE_EXTRA_CA_CERTS"] == CA_TARGET


def test_an_agent_override_cannot_replace_the_ca_mount(env: Env, mkcert: FakeMkcert) -> None:
    with env.config_file.open("a") as config:
        config.write(
            "agent:\n  services:\n    agent:\n      volumes:\n"
            f"        - /tmp/evil.pem:{CA_TARGET}:ro\n"
        )
    git = FakeGit()
    make_repo(env, git, "web", WEB)

    result = invoke(["jira-123", "up", "--repos", "web"], git, FakeCompose(), mkcert=mkcert)

    assert result.exit_code == 0, result.output
    agent_volumes = services(env)["agent"]["volumes"]
    assert [v for v in agent_volumes if "evil" in str(v)] == []
    assert mounts(services(env)["agent"])[CA_TARGET]["source"] == str(mkcert.ca_dir / "rootCA.pem")


def test_up_fails_early_naming_the_fix_when_root_ca_is_missing(
    env: Env, mkcert: FakeMkcert
) -> None:
    (mkcert.ca_dir / "rootCA.pem").unlink()
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", WEB)

    result = invoke(["jira-123", "up", "--repos", "web"], git, compose, mkcert=mkcert)

    assert result.exit_code == 1
    assert "rootCA.pem" in result.output
    assert "sudo sandbox setup" in result.output
    assert compose.calls == []
    assert mkcert.issued == []
    assert not env.set_dir("jira-123").exists()


def test_a_set_without_web_apps_needs_no_ca(env: Env, mkcert: FakeMkcert) -> None:
    (mkcert.ca_dir / "rootCA.pem").unlink()
    git = FakeGit()
    make_repo(env, git, "api", "services:\n  mysql:\n    image: mysql:8.4\n")

    up(env, git, FakeCompose(), mkcert, "api")

    rendered = services(env)
    assert CA_TARGET not in mounts(rendered["agent"])
    assert CA_TARGET not in mounts(rendered["mysql"])
    assert not (env.set_dir("jira-123") / "tls").exists()


@pytest.mark.parametrize(
    ("compose", "message"),
    [
        (
            WEB + "  admin:\n    image: nginx\n    x-sandbox: {web: true}\n",
            "more than one web service (admin, web)",
        ),
        (WEB.replace("  web:\n", "  frontend:\n"), "web service 'frontend' must be named after"),
        (WEB.replace("web: true", "web: yes please"), "x-sandbox.web: expected true or false"),
        (
            WEB.replace("    x-sandbox:\n      web: true\n", "    x-sandbox: true\n"),
            "expected a mapping",
        ),
        (WEB + "    network_mode: host\n", "cannot set network_mode"),
    ],
)
def test_malformed_web_apps_fail_before_any_container_starts(
    env: Env, mkcert: FakeMkcert, compose: str, message: str
) -> None:
    git, fake = FakeGit(), FakeCompose()
    make_repo(env, git, "web", compose)

    result = invoke(["jira-123", "up", "--repos", "web"], git, fake, mkcert=mkcert)

    assert result.exit_code == 1
    assert message in result.output
    assert fake.calls == []
    assert mkcert.issued == []


def test_up_fails_if_a_hostname_is_used_twice_in_the_set(env: Env, mkcert: FakeMkcert) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "web", WEB.replace('    environment:\n      DEBUG: "1"\n', ""))
    # Another repo runs a service under the web app's name: web.jira-123.test twice.
    make_repo(env, git, "api", "services:\n  web:\n    image: python:3.12-alpine\n")

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git, compose, mkcert=mkcert)

    assert result.exit_code == 1
    assert (
        "hostname web.jira-123.test is used more than once in set 'jira-123': "
        "web app of repo web; service 'web' of repo api"
    ) in result.output
    assert compose.calls == []
