"""`up` checks what `sudo sandbox setup` installs before it creates anything.

Driven through the CLI seam with a fake host (``tests.fakes_host``): its root is a temp
directory, and `sudo -n -l` and `ifconfig lo0` answers come from a table.
"""

from pathlib import Path

import pytest

from sandbox.platforms import Host
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose
from tests.fakes_host import FakeRunner, lo0_with_aliases, make_host

MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n"
HELPER = "usr/local/bin/sandbox-hosts"
SUDOERS = "etc/sudoers.d/sandbox-hosts"
PLIST = "Library/LaunchDaemons/sandbox.loopback-aliases.plist"


def use_system_hosts_file(env: Env) -> None:
    """Production config: the hosts block goes through `sudo -n sandbox-hosts`."""
    text = env.config_file.read_text()
    env.config_file.write_text(
        "".join(line for line in text.splitlines(keepends=True) if "hosts_file" not in line)
    )


def make_repo(env: Env, git: FakeGit, name: str = "api", compose: str | None = MYSQL) -> None:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    if compose is not None:
        (path / ".sandbox" / "compose.yaml").write_text(compose)


def install(root: Path, *relative: str) -> None:
    for name in relative:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("installed\n")


def up(env: Env, git: FakeGit, compose: FakeCompose, host: Host, *repos: str):
    return invoke(["jira-123", "up", "--repos", *(repos or ("api",))], git, compose, host=host)


def assert_nothing_created(env: Env, compose: FakeCompose) -> None:
    assert compose.calls == []
    assert not env.set_dir("jira-123").exists()
    assert not (env.state_dir / "leases.json").exists()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "machine"
    path.mkdir()
    return path


# --- the hosts helper ----------------------------------------------------------------------


def test_up_fails_before_creating_anything_when_the_helper_is_missing(env: Env, root: Path) -> None:
    use_system_hosts_file(env)
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = up(env, git, compose, make_host(root))

    assert result.exit_code == 1
    assert "hosts helper /usr/local/bin/sandbox-hosts is not installed" in result.output
    assert "sudo sandbox setup" in result.output
    assert_nothing_created(env, compose)


def test_up_fails_when_the_helpers_sudoers_rule_is_missing(env: Env, root: Path) -> None:
    use_system_hosts_file(env)
    install(root, HELPER)
    runner = FakeRunner()
    runner.answer(["sudo", "-n", "-l"], 1, "sudo: a password is required")
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = up(env, git, compose, make_host(root, runner=runner))

    assert result.exit_code == 1
    assert "no passwordless sudo rule for /usr/local/bin/sandbox-hosts" in result.output
    assert "sudo sandbox setup" in result.output
    assert runner.ran("sudo", "-n", "-l") == [["sudo", "-n", "-l", "/usr/local/bin/sandbox-hosts"]]
    assert_nothing_created(env, compose)


def test_an_installed_helper_with_its_rule_passes(env: Env, root: Path) -> None:
    use_system_hosts_file(env)
    install(root, HELPER)
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)
    # The hosts block itself is written through `sudo -n`; stop just after the check.
    compose.up_error = "stop here"

    result = up(env, git, compose, make_host(root))

    assert "hosts helper" not in result.output
    assert "stop here" in result.output


def test_a_configured_hosts_file_needs_no_helper(env: Env, root: Path) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = up(env, git, compose, make_host(root))

    assert result.exit_code == 0, result.output


def test_a_set_without_services_needs_no_helper(env: Env, root: Path) -> None:
    use_system_hosts_file(env)
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, compose=None)

    result = up(env, git, compose, make_host(root))

    assert result.exit_code == 0, result.output


# --- the macOS loopback alias pool ---------------------------------------------------------


def macos(root: Path, *aliased: str, plist: bool = True) -> tuple[Host, FakeRunner]:
    runner = FakeRunner()
    runner.answer(["ifconfig", "lo0"], 0, lo0_with_aliases(*aliased))
    if plist:
        install(root, PLIST)
    return make_host(root, platform="darwin", runner=runner), runner


def test_on_macos_up_fails_before_creating_anything_without_the_launchd_job(
    env: Env, root: Path
) -> None:
    host, _ = macos(root, plist=False)
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = up(env, git, compose, host)

    assert result.exit_code == 1
    assert "loopback alias pool is not installed" in result.output
    assert "sudo sandbox setup" in result.output
    assert_nothing_created(env, compose)


def test_on_macos_up_fails_when_the_job_is_installed_but_no_alias_exists(
    env: Env, root: Path
) -> None:
    host, _ = macos(root)
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = up(env, git, compose, host)

    assert result.exit_code == 1
    assert "no lo0 aliases in 127.42.0.0/16" in result.output
    assert_nothing_created(env, compose)


def test_on_macos_a_set_comes_up_with_the_alias_pool_installed(env: Env, root: Path) -> None:
    host, _ = macos(root, "127.42.0.0/24")
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    # More sets than aliased blocks: nothing is leased, so the pool's size limits nothing.
    results = [
        invoke([name, "up", "--repos", "api"], git, compose, host=host) for name in ("a", "b", "c")
    ]

    assert [result.exit_code for result in results] == [0, 0, 0], results[-1].output
    assert "mysql.c.test" in results[-1].output


def test_on_linux_up_reads_no_loopback_state(env: Env, root: Path) -> None:
    runner = FakeRunner()
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = up(env, git, compose, make_host(root, runner=runner))

    assert result.exit_code == 0, result.output
    assert runner.calls == []


def test_on_macos_a_set_without_services_needs_no_alias_pool(env: Env, root: Path) -> None:
    host, runner = macos(root, plist=False)
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, compose=None)

    result = up(env, git, compose, host)

    assert result.exit_code == 0, result.output
    assert runner.calls == []
