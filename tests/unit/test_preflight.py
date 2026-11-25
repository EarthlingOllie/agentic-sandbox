"""`up` checks, before it creates anything, that this machine can reach the set by name.

Driven through the CLI seam with a fake host (``tests.fakes_host``) and the fake Docker: the
host's root is a temp directory holding whatever per-domain resolver configuration the test
installs, `orbctl` and `systemctl` answers come from a table, and the fake Docker says which
runtime it is.
"""

from pathlib import Path

import pytest
from typer.testing import Result

from sandbox.platforms import Host
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_certs import FakeMkcert
from tests.fakes_compose import FakeCompose
from tests.fakes_docker import FakeDocker
from tests.fakes_host import FakeRunner, make_host, point_at_resolver

MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n"
WEB = "services:\n  api:\n    image: nginx\n    x-sandbox: {web: true}\n"
HELPER = "usr/local/bin/sandbox-hosts"
SUDOERS = "etc/sudoers.d/sandbox-hosts"
PLIST = "Library/LaunchDaemons/sandbox.loopback-aliases.plist"
ORBSTACK_SETTINGS = "rosetta: true\nnetwork_bridge: {}\nmemory_mib: 8192\n"


def configure_domain(env: Env, domain: str) -> None:
    with env.config_file.open("a") as config:
        config.write(f"domain: {domain}\n")


def make_repo(env: Env, git: FakeGit, name: str = "api", compose: str | None = MYSQL) -> None:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    if compose is not None:
        (path / ".sandbox" / "compose.yaml").write_text(compose)


class Machine:
    """One `up` of jira-123 on a fake machine, and what it left behind."""

    def __init__(self, env: Env, root: Path, platform: str = "linux") -> None:
        self.env = env
        self.root = root
        self.platform = platform
        self.git = FakeGit()
        self.compose = FakeCompose()
        self.docker = FakeDocker(self.compose)
        self.runner = FakeRunner()
        self.mkcert: FakeMkcert | None = None

    @property
    def host(self) -> Host:
        return make_host(self.root, platform=self.platform, runner=self.runner)

    def set_up(self, domain: str = "test") -> None:
        """What `sudo sandbox setup` leaves on the machine for ``domain``."""
        point_at_resolver(self.root, domain, platform=self.platform)

    def up(self, compose: str | None = MYSQL) -> Result:
        make_repo(self.env, self.git, compose=compose)
        return invoke(
            ["jira-123", "up", "--repos", "api"],
            self.git,
            self.compose,
            self.docker,
            mkcert=self.mkcert,
            host=self.host,
        )

    def assert_nothing_created(self) -> None:
        assert self.compose.calls == []
        assert self.git.clones == {}
        assert not self.env.set_dir("jira-123").exists()
        assert self.docker.resolver_starts == 0
        assert self.docker.resolver_networks == set()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "machine"
    path.mkdir()
    return path


@pytest.fixture
def linux(env: Env, root: Path) -> Machine:
    return Machine(env, root)


@pytest.fixture
def mac(env: Env, root: Path) -> Machine:
    return Machine(env, root, platform="darwin")


# --- the per-domain resolver configuration -------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_up_fails_before_creating_anything_on_a_machine_setup_never_ran_on(
    env: Env, root: Path, platform: str
) -> None:
    machine = Machine(env, root, platform)

    result = machine.up()

    assert result.exit_code == 1
    assert "does not send names under `test` to sandbox's resolver" in result.output
    assert "run `sudo sandbox setup`" in result.output
    machine.assert_nothing_created()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_a_machine_setup_ran_on_brings_the_set_up(env: Env, root: Path, platform: str) -> None:
    machine = Machine(env, root, platform)
    machine.set_up()

    result = machine.up()

    assert result.exit_code == 0, result.output
    assert "mysql.jira-123.test" in result.output
    assert machine.docker.resolver_networks == {"sandbox-jira-123_default"}


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_up_fails_naming_both_domains_when_the_configured_one_is_not_the_installed_one(
    env: Env, root: Path, platform: str
) -> None:
    machine = Machine(env, root, platform)
    machine.set_up("test")
    configure_domain(env, "dev.example")

    result = machine.up()

    assert result.exit_code == 1
    assert "the configured domain is `dev.example`" in result.output
    assert "setup pointed this machine at the resolver for `test`" in result.output
    assert "rerun setup: `sudo sandbox setup --domain dev.example`" in result.output
    machine.assert_nothing_created()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_a_non_default_domain_setup_installed_passes(env: Env, root: Path, platform: str) -> None:
    machine = Machine(env, root, platform)
    machine.set_up("dev.example")
    configure_domain(env, "dev.example")

    result = machine.up()

    assert result.exit_code == 0, result.output
    assert "mysql.jira-123.dev.example" in result.output


def test_on_macos_another_tools_resolver_file_for_the_domain_does_not_count(mac: Machine) -> None:
    other = mac.root / "etc" / "resolver" / "test"
    other.parent.mkdir(parents=True)
    other.write_text("nameserver 10.0.0.53\n")

    result = mac.up()

    assert result.exit_code == 1
    assert "run `sudo sandbox setup`" in result.output
    mac.assert_nothing_created()


def test_on_linux_without_systemd_resolved_up_cannot_check_and_says_so(linux: Machine) -> None:
    """Setup installs nothing there (the developer adds one line to their own DNS server),
    so there is nothing `up` could read: it warns and carries on."""
    linux.runner.answer(["systemctl", "is-active"], 3, "inactive")

    result = linux.up()

    assert result.exit_code == 0, result.output
    assert "warning: cannot check that this machine sends names under `test`" in result.output
    assert "server=/test/127.0.0.1#15353" in result.output


def test_on_linux_with_the_configuration_installed_up_runs_no_command(linux: Machine) -> None:
    linux.set_up()

    result = linux.up()

    assert result.exit_code == 0, result.output
    assert linux.runner.calls == []
    assert "warning" not in result.output


# --- macOS: the Docker runtime -------------------------------------------------------------


@pytest.mark.parametrize(
    "runtime", ["Docker Desktop", "Colima", "Rancher Desktop WSL Distribution"]
)
def test_on_macos_a_runtime_other_than_orbstack_fails_naming_orbstack(
    mac: Machine, runtime: str
) -> None:
    mac.set_up()
    mac.docker.runtime = runtime

    result = mac.up()

    assert result.exit_code == 1
    assert f"Docker on this Mac is {runtime}" in result.output
    assert "containers are not reachable at their own addresses" in result.output
    assert "OrbStack (https://orbstack.dev)" in result.output
    mac.assert_nothing_created()


def test_on_macos_with_orbstack_and_direct_container_access_off_up_names_the_setting(
    mac: Machine,
) -> None:
    mac.set_up()
    mac.runner.answer(["orbctl", "config", "show"], 0, ORBSTACK_SETTINGS.format("false"))

    result = mac.up()

    assert result.exit_code == 1
    assert "OrbStack's direct container access is off" in result.output
    assert "Allow access to container domains & IPs" in result.output
    assert "orbctl config set network_bridge true" in result.output
    mac.assert_nothing_created()


def test_on_macos_with_orbstack_and_direct_container_access_on_the_set_comes_up(
    mac: Machine,
) -> None:
    mac.set_up()
    mac.runner.answer(["orbctl", "config", "show"], 0, ORBSTACK_SETTINGS.format("true"))

    result = mac.up()

    assert result.exit_code == 0, result.output
    assert mac.runner.calls == [["orbctl", "config", "show"]]


def test_on_macos_an_orbstack_that_does_not_report_the_setting_passes(mac: Machine) -> None:
    mac.set_up()
    mac.runner.answer(["orbctl"], 127, "No such file or directory: 'orbctl'")

    result = mac.up()

    assert result.exit_code == 0, result.output


def test_on_macos_up_names_everything_that_is_wrong_at_once(mac: Machine) -> None:
    mac.docker.runtime = "Docker Desktop"

    result = mac.up()

    assert result.exit_code == 1
    assert "Docker on this Mac is Docker Desktop" in result.output
    assert "run `sudo sandbox setup`" in result.output
    mac.assert_nothing_created()


def test_on_linux_the_docker_runtime_is_not_asked_about(linux: Machine) -> None:
    linux.set_up()
    linux.docker.runtime = "Docker Desktop"

    result = linux.up()

    assert result.exit_code == 0, result.output


# --- what needs none of it, and what is still needed ---------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_a_set_without_services_needs_none_of_it(env: Env, root: Path, platform: str) -> None:
    machine = Machine(env, root, platform)
    machine.docker.runtime = "Docker Desktop"
    configure_domain(env, "dev.example")

    result = machine.up(compose=None)

    assert result.exit_code == 0, result.output
    assert machine.runner.calls == []
    assert "warning" not in result.output


def test_a_set_with_a_web_app_still_needs_the_mkcert_ca(linux: Machine) -> None:
    linux.set_up()
    linux.mkcert = FakeMkcert(linux.env.root / "mkcert")
    (linux.mkcert.ca_dir / "rootCA.pem").unlink()

    result = linux.up(compose=WEB)

    assert result.exit_code == 1
    assert "rootCA.pem" in result.output
    assert "sudo sandbox setup" in result.output
    linux.assert_nothing_created()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_hosts_helper_its_sudoers_rule_and_the_alias_pool_are_not_checked(
    env: Env, root: Path, platform: str
) -> None:
    """None of the three is installed, the config names no hosts file, and `sudo -n -l` and
    `ifconfig lo0` would both fail: `up` asks neither."""
    env.config_file.write_text(
        "".join(
            line
            for line in env.config_file.read_text().splitlines(keepends=True)
            if "hosts_file" not in line
        )
    )
    machine = Machine(env, root, platform)
    machine.set_up()
    machine.runner.answer(["sudo"], 1, "sudo: a password is required")
    machine.runner.answer(["ifconfig"], 1, "ifconfig: interface lo0 does not exist")

    result = machine.up()

    assert result.exit_code == 0, result.output
    assert not any((root / name).exists() for name in (HELPER, SUDOERS, PLIST))
    assert machine.runner.ran("sudo") == []
    assert machine.runner.ran("ifconfig") == []
