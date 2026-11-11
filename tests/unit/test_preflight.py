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


def test_on_macos_a_set_leases_inside_the_alias_pool(env: Env, root: Path) -> None:
    host, _ = macos(root, "127.42.0.0/24", "127.42.1.0/24")
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    first = invoke(["a", "up", "--repos", "api"], git, compose, host=host)
    second = invoke(["b", "up", "--repos", "api"], git, compose, host=host)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "mysql.b.test  127.42.1.1" in second.output


def test_on_macos_up_fails_clearly_when_every_alias_block_is_leased(env: Env, root: Path) -> None:
    host, _ = macos(root, "127.42.0.0/24", "127.42.1.0/24")
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)
    for name in ("a", "b"):
        assert invoke([name, "up", "--repos", "api"], git, compose, host=host).exit_code == 0

    result = invoke(["c", "up", "--repos", "api"], git, compose, host=host)

    assert result.exit_code == 1
    assert "all 2 loopback alias blocks" in result.output
    assert "sudo sandbox setup --sets" in result.output
    assert not env.set_dir("c").exists()


def test_on_macos_a_set_whose_block_lost_its_aliases_fails(env: Env, root: Path) -> None:
    """Setup rerun with a smaller --sets leaves an existing lease outside the pool."""
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)
    wide, _ = macos(root, "127.42.0.0/24", "127.42.1.0/24")
    for name in ("a", "b"):
        assert invoke([name, "up", "--repos", "api"], git, compose, host=wide).exit_code == 0
    assert invoke(["b", "down"], git, compose).exit_code == 0
    narrow, _ = macos(root, "127.42.0.0/24")

    result = invoke(["b", "up"], git, compose, host=narrow)

    assert result.exit_code == 1
    assert "127.42.1.0/24 has no lo0 aliases" in result.output
    assert "sudo sandbox setup --sets 2" in result.output


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
