"""`sudo sandbox setup [--domain X]` points the machine at the resolver for the domain.

Driven through the CLI seam with the fake host (``tests.fakes_host``) and the fake Docker:
the per-domain resolver configuration is read back from the fake machine's filesystem, and
the commands setup would run (`systemctl`, `sudo -u ...`) are recorded.
"""

from pathlib import Path
from typing import Any

import pytest

from sandbox.platforms import Host
from sandbox.resolver_config import installed_domain
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_docker import FakeDocker
from tests.fakes_host import FakeRunner, make_host

MACOS_RESOLVER_DIR = "etc/resolver"
DROP_IN = "etc/systemd/resolved.conf.d/sandbox.conf"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "machine"
    (path / "etc").mkdir(parents=True)
    (path / "etc" / "hosts").write_text("127.0.0.1\tlocalhost\n")
    return path


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    mkcert = path / "mkcert"
    mkcert.write_text("#!/bin/sh\nexit 0\n")
    mkcert.chmod(0o755)
    return path


def linux(root: Path, bin_dir: Path, **kwargs: Any) -> tuple[Host, FakeRunner]:  # noqa: ANN401
    runner = FakeRunner()
    return make_host(root, path=str(bin_dir), runner=runner, **kwargs), runner


def macos(root: Path, bin_dir: Path) -> tuple[Host, FakeRunner]:
    runner = FakeRunner()
    return make_host(root, path=str(bin_dir), platform="darwin", runner=runner), runner


def setup(h: Host, *args: str, docker: FakeDocker | None = None):
    return invoke(["setup", *args], FakeGit(), host=h, docker=docker)


def resolver_files(root: Path) -> dict[str, str]:
    """macOS: each per-domain resolver file on the machine, by domain."""
    directory = root / MACOS_RESOLVER_DIR
    if not directory.is_dir():
        return {}
    return {path.name: path.read_text() for path in directory.iterdir()}


def settings(text: str) -> list[str]:
    """The lines of a configuration file that are not comments or blank."""
    return [
        line.strip() for line in text.splitlines() if line.strip() and line.strip()[0] not in "#;"
    ]


# --- macOS -----------------------------------------------------------------------------------


def test_on_macos_setup_installs_the_per_domain_resolver_file(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = macos(root, bin_dir)

    result = setup(h)

    assert result.exit_code == 0, result.output
    files = resolver_files(root)
    assert list(files) == ["test"]
    assert settings(files["test"]) == ["nameserver 127.0.0.1", "port 15353"]
    assert (root / MACOS_RESOLVER_DIR / "test").stat().st_mode & 0o777 == 0o644


# --- Linux -----------------------------------------------------------------------------------

RELOAD = ["systemctl", "reload-or-restart", "systemd-resolved.service"]


def test_on_linux_with_systemd_resolved_setup_installs_a_drop_in_and_reloads(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = linux(root, bin_dir)

    result = setup(h)

    assert result.exit_code == 0, result.output
    assert settings((root / DROP_IN).read_text()) == [
        "[Resolve]",
        "DNS=127.0.0.1:15353",
        "Domains=~test",
    ]
    assert (root / DROP_IN).stat().st_mode & 0o777 == 0o644
    assert runner.ran("systemctl", "reload-or-restart") == [RELOAD]
    assert not (root / MACOS_RESOLVER_DIR).exists()


def without_systemd_resolved(root: Path, bin_dir: Path) -> tuple[Host, FakeRunner]:
    h, runner = linux(root, bin_dir)
    runner.answer(["systemctl"], 3, "inactive")
    return h, runner


def installed_files(root: Path) -> list[str]:
    """Every file on the fake machine besides the hosts file it started with."""
    files = (path for path in root.rglob("*") if path.is_file())
    return sorted(str(path.relative_to(root)) for path in files if path != root / "etc" / "hosts")


def test_on_linux_without_systemd_resolved_setup_refuses_before_installing_anything(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = without_systemd_resolved(root, bin_dir)

    result = setup(h, "--domain", "dev.test")

    assert result.exit_code == 1
    assert "systemd-resolved is not running" in result.output
    assert "server=/dev.test/127.0.0.1#15353" in result.output
    assert installed_files(root) == []
    assert runner.ran("sudo") == []  # no mkcert CA, no write to the user's config
    assert runner.ran("systemctl", "reload-or-restart") == []


# --- the domain ------------------------------------------------------------------------------


def user_config(root: Path) -> Path:
    return root / "home" / "dev" / ".config" / "sandbox" / "config.yaml"


def writes_as(runner: FakeRunner, user: str) -> list[list[str]]:
    return runner.ran("sudo", "-u", user, "-H", "--", "/bin/sh")


def test_setup_with_a_domain_writes_it_into_the_users_config_keeping_the_other_keys(
    env: Env, root: Path, bin_dir: Path
) -> None:
    config = user_config(root)
    config.parent.mkdir(parents=True)
    config.write_text(
        "# where my sets live\nsets_root: ~/work/sets\n"
        "domain: old.test  # was\ntemplates_root: ~/plans\n"
    )
    h, runner = linux(root, bin_dir)

    result = setup(h, "--domain", "dev.test")

    assert result.exit_code == 0, result.output
    assert config.read_text() == (
        "# where my sets live\nsets_root: ~/work/sets\ndomain: dev.test\ntemplates_root: ~/plans\n"
    )
    assert len(writes_as(runner, "dev")) == 1  # written as the user, never by root
    assert "Domains=~dev.test" in (root / DROP_IN).read_text()


def test_setup_with_a_domain_creates_the_users_config_when_there_is_none(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = macos(root, bin_dir)

    result = setup(h, "--domain", "sandbox.test")

    assert result.exit_code == 0, result.output
    assert user_config(root).read_text() == "domain: sandbox.test\n"
    assert len(writes_as(runner, "dev")) == 1
    assert list(resolver_files(root)) == ["sandbox.test"]


def test_setup_with_a_domain_adds_it_to_a_config_without_one(
    env: Env, root: Path, bin_dir: Path
) -> None:
    config = user_config(root)
    config.parent.mkdir(parents=True)
    config.write_text("agent:\n  services:\n    agent:\n      environment:\n        A: b")
    h, _ = linux(root, bin_dir)

    result = setup(h, "--domain", "dev.test")

    assert result.exit_code == 0, result.output
    assert config.read_text() == (
        "agent:\n  services:\n    agent:\n      environment:\n        A: b\ndomain: dev.test\n"
    )


def test_a_domain_yaml_would_not_read_as_text_is_written_quoted(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = linux(root, bin_dir)

    result = setup(h, "--domain", "123")

    assert result.exit_code == 0, result.output
    assert user_config(root).read_text() == "domain: '123'\n"
    assert "Domains=~123" in (root / DROP_IN).read_text()


def test_without_a_domain_setup_uses_the_configured_one_and_leaves_the_config_alone(
    env: Env, root: Path, bin_dir: Path
) -> None:
    config = user_config(root)
    config.parent.mkdir(parents=True)
    config.write_text("domain: mine.test\n")
    h, runner = macos(root, bin_dir)

    result = setup(h)

    assert result.exit_code == 0, result.output
    assert list(resolver_files(root)) == ["mine.test"]
    assert config.read_text() == "domain: mine.test\n"
    assert writes_as(runner, "dev") == []


def test_without_a_domain_or_a_config_setup_uses_test_and_writes_no_config(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = linux(root, bin_dir)

    result = setup(h)

    assert result.exit_code == 0, result.output
    assert "Domains=~test" in (root / DROP_IN).read_text()
    assert not user_config(root).exists()


@pytest.mark.parametrize("domain", ["local", "Dev.Test", "a..b", "-x.test"])
def test_a_domain_set_hostnames_cannot_end_in_is_refused_before_installing_anything(
    env: Env, root: Path, bin_dir: Path, domain: str
) -> None:
    h, runner = linux(root, bin_dir)

    result = setup(h, "--domain", domain)

    assert result.exit_code == 1
    assert "--domain" in result.output
    assert installed_files(root) == []
    assert runner.calls == []


def test_a_config_that_cannot_be_written_as_the_user_fails_setup(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = linux(root, bin_dir)
    runner.answer(["sudo", "-u", "dev", "-H", "--", "/bin/sh"], 1, "Permission denied")

    result = setup(h, "--domain", "dev.test")

    assert result.exit_code == 1
    assert "could not write `domain: dev.test`" in result.output
    assert "Permission denied" in result.output
    assert not (root / DROP_IN).exists()


def test_a_config_a_line_edit_would_not_be_safe_on_is_rewritten_with_every_key(
    env: Env, root: Path, bin_dir: Path
) -> None:
    config = user_config(root)
    config.parent.mkdir(parents=True)
    config.write_text('{"domain": old.test, sets_root: /srv/sets}\n')
    h, _ = linux(root, bin_dir)

    result = setup(h, "--domain", "dev.test")

    assert result.exit_code == 0, result.output
    assert config.read_text() == "domain: dev.test\nsets_root: /srv/sets\n"


# --- changing the domain, and the record of it -----------------------------------------------


def test_on_macos_a_different_domain_replaces_the_previous_domains_file(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = macos(root, bin_dir)
    assert setup(h).exit_code == 0
    other = root / MACOS_RESOLVER_DIR / "corp"
    other.write_text("nameserver 10.0.0.53\n")  # another tool's: not sandbox's to remove

    result = setup(h, "--domain", "dev.test")

    assert result.exit_code == 0, result.output
    files = resolver_files(root)
    assert sorted(files) == ["corp", "dev.test"]
    assert settings(files["dev.test"]) == ["nameserver 127.0.0.1", "port 15353"]
    assert files["corp"] == "nameserver 10.0.0.53\n"
    assert "removed /etc/resolver/test" in result.output


def test_on_linux_a_different_domain_replaces_the_previous_domains_drop_in(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = linux(root, bin_dir)
    assert setup(h).exit_code == 0
    runner.calls.clear()

    result = setup(h, "--domain", "dev.test")

    assert result.exit_code == 0, result.output
    drop_ins = list((root / DROP_IN).parent.iterdir())
    assert drop_ins == [root / DROP_IN]
    assert [line for line in settings(drop_ins[0].read_text()) if line.startswith("Domains")] == [
        "Domains=~dev.test"
    ]
    assert runner.ran("systemctl", "reload-or-restart") == [RELOAD]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_setup_records_the_domain_it_installed_where_an_unprivileged_up_can_read_it(
    env: Env, root: Path, bin_dir: Path, platform: str
) -> None:
    h, _ = macos(root, bin_dir) if platform == "darwin" else linux(root, bin_dir)
    unprivileged = make_host(root, platform=platform, euid=1000, sudo_user=None)
    assert installed_domain(unprivileged) is None

    assert setup(h, "--domain", "dev.test").exit_code == 0
    assert installed_domain(unprivileged) == "dev.test"

    assert setup(h, "--domain", "other.test").exit_code == 0
    assert installed_domain(unprivileged) == "other.test"


# --- what setup does not do, and still does ----------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_setup_starts_no_containers_and_installs_the_ca_as_the_invoking_user(
    env: Env, root: Path, bin_dir: Path, platform: str
) -> None:
    h, runner = macos(root, bin_dir) if platform == "darwin" else linux(root, bin_dir)
    docker = FakeDocker()

    result = setup(h, "--domain", "dev.test", docker=docker)

    assert result.exit_code == 0, result.output
    assert docker.resolver_starts == 0
    assert docker.resolver_domain is None
    assert docker.compose.running == {}
    assert runner.ran("docker") == []
    mkcert = str(bin_dir / "mkcert")
    assert [call for call in runner.calls if mkcert in call] == [
        ["sudo", "-u", "dev", "-H", "--", mkcert, "-install"]
    ]


def test_on_linux_the_summary_names_what_setup_installed(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = linux(root, bin_dir)

    result = setup(h, "--domain", "dev.test")

    assert result.exit_code == 0, result.output
    assert "sandbox setup installed:" in result.output
    assert f"config: `domain: dev.test` written to {user_config(root)} as dev" in result.output
    assert (
        "resolver: names under .dev.test are looked up at 127.0.0.1 port 15353 "
        "(/etc/systemd/resolved.conf.d/sandbox.conf, systemd-resolved.service reloaded)"
    ) in result.output
    assert "mkcert: CA installed for dev" in result.output


def test_on_macos_the_summary_names_what_setup_installed(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = macos(root, bin_dir)

    result = setup(h)

    assert result.exit_code == 0, result.output
    assert (
        "resolver: names under .test are looked up at 127.0.0.1 port 15353 (/etc/resolver/test)"
    ) in result.output
    assert "config:" not in result.output  # no --domain: the config was not written
    assert "mkcert: CA installed for dev" in result.output


# --- uninstall -------------------------------------------------------------------------------


def running_resolver(domain: str = "dev.test") -> FakeDocker:
    docker = FakeDocker()
    docker.resolver_domain = domain
    docker.resolver_networks.add("sandbox-jira-123_default")
    return docker


def test_on_macos_uninstall_removes_the_installed_domains_file_and_the_resolver(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = macos(root, bin_dir)
    assert setup(h, "--domain", "dev.test").exit_code == 0
    (root / MACOS_RESOLVER_DIR / "corp").write_text("nameserver 10.0.0.53\n")
    user_config(root).write_text("domain: changed-since.test\n")  # not what was installed
    docker = running_resolver()
    runner.calls.clear()

    result = setup(h, "--uninstall", docker=docker)

    assert result.exit_code == 0, result.output
    assert list(resolver_files(root)) == ["corp"]
    assert docker.resolver_domain is None
    assert docker.resolver_networks == set()
    assert docker.resolver_removals == ["dev"]  # with the user's Docker, not root's
    assert "removed /etc/resolver/dev.test" in result.output
    assert "removed the resolver (container sandbox_resolver)" in result.output


def test_on_linux_uninstall_removes_the_drop_in_reloads_and_removes_the_resolver(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = linux(root, bin_dir)
    assert setup(h, "--domain", "dev.test").exit_code == 0
    docker = running_resolver()
    runner.calls.clear()

    result = setup(h, "--uninstall", docker=docker)

    assert result.exit_code == 0, result.output
    assert not (root / DROP_IN).exists()
    assert runner.ran("systemctl", "reload-or-restart") == [RELOAD]
    assert docker.resolver_domain is None
    assert installed_domain(h) is None
    assert "removed /etc/systemd/resolved.conf.d/sandbox.conf" in result.output


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_uninstall_leaves_the_mkcert_ca_and_says_how_to_remove_it(
    env: Env, root: Path, bin_dir: Path, platform: str
) -> None:
    h, runner = macos(root, bin_dir) if platform == "darwin" else linux(root, bin_dir)
    assert setup(h).exit_code == 0
    runner.calls.clear()

    result = setup(h, "--uninstall", docker=FakeDocker())

    assert result.exit_code == 0, result.output
    assert not [call for call in runner.calls if any("mkcert" in arg for arg in call)]
    assert "the mkcert CA was left installed" in result.output
    assert "mkcert -uninstall" in result.output


def test_uninstall_with_nothing_installed_reloads_nothing_and_succeeds(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = linux(root, bin_dir)
    docker = FakeDocker()

    result = setup(h, "--uninstall", docker=docker)

    assert result.exit_code == 0, result.output
    assert runner.ran("systemctl") == []
    assert docker.resolver_removals == ["dev"]


def test_uninstall_from_a_root_shell_removes_the_resolver_as_root(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = linux(root, bin_dir, sudo_user=None)
    docker = running_resolver()

    result = setup(h, "--uninstall", docker=docker)

    assert result.exit_code == 0, result.output
    assert docker.resolver_removals == [None]


def test_uninstall_that_cannot_remove_the_resolver_fails_after_removing_the_rest(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = linux(root, bin_dir)
    assert setup(h).exit_code == 0
    docker = running_resolver("test")
    docker.resolver_removal_error = "Cannot connect to the Docker daemon"

    result = setup(h, "--uninstall", docker=docker)

    assert result.exit_code == 1
    assert not (root / DROP_IN).exists()
    assert "the resolver could not be removed" in result.output
    assert "Cannot connect to the Docker daemon" in result.output
    assert "docker rm --force sandbox_resolver" in result.output


def test_uninstall_takes_no_domain(env: Env, root: Path, bin_dir: Path) -> None:
    h, _ = linux(root, bin_dir)
    assert setup(h).exit_code == 0

    result = setup(h, "--uninstall", "--domain", "dev.test", docker=FakeDocker())

    assert result.exit_code == 2
    assert "--uninstall takes no --domain" in result.output
    assert (root / DROP_IN).exists()
