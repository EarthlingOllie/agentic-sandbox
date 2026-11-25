"""`sudo sandbox setup [--uninstall]` installs what sandbox needs root for, and removes what
an earlier version installed and nothing uses any more.

Driven through the CLI seam with a fake host (``tests.fakes_host``): it is "root", its
filesystem root is a temp directory, and every command it would run (`launchctl`,
`systemctl`, `sudo -u ... mkcert`, the script taking `lo0` aliases away) is recorded and
answered from a table. Pointing the machine at the resolver is in ``test_setup_resolver``.
"""

import plistlib
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sandbox.platforms import Host
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_host import FakeRunner, make_host

# What the loopback model installed (literal paths: this is what is on upgraded machines).
HELPER = "usr/local/bin/sandbox-hosts"
BUNDLE = "usr/local/lib/sandbox-hosts"
SUDOERS = "etc/sudoers.d/sandbox-hosts"
PLIST = "Library/LaunchDaemons/sandbox.loopback-aliases.plist"
LABEL = "sandbox.loopback-aliases"
HOSTS = "etc/hosts"
OWN_HOSTS = "127.0.0.1\tlocalhost\n::1 ip6-localhost\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "machine"
    (path / "etc").mkdir(parents=True)
    (path / HOSTS).write_text(OWN_HOSTS)
    return path


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """A PATH directory holding a `mkcert` (setup only looks it up; the runner is fake)."""
    path = tmp_path / "bin"
    path.mkdir()
    mkcert = path / "mkcert"
    mkcert.write_text("#!/bin/sh\nexit 0\n")
    mkcert.chmod(0o755)
    return path


def host(root: Path, bin_dir: Path, **kwargs: Any) -> Host:  # noqa: ANN401 - make_host options
    return make_host(root, path=str(bin_dir), **kwargs)


def setup(h: Host, *args: str):
    return invoke(["setup", *args], FakeGit(), host=h)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def assert_nothing_installed(root: Path) -> None:
    for relative in (HELPER, BUNDLE, SUDOERS, PLIST):
        assert not (root / relative).exists(), relative


# --- who may run it ------------------------------------------------------------------------


def test_setup_needs_root(env: Env, root: Path, bin_dir: Path) -> None:
    runner = FakeRunner()

    result = setup(host(root, bin_dir, euid=1000, runner=runner))

    assert result.exit_code == 1
    assert "setup needs root: run `sudo sandbox setup`" in result.output
    assert runner.calls == []
    assert_nothing_installed(root)


@pytest.mark.parametrize("sudo_user", [None, "root"])
def test_setup_needs_the_invoking_user(
    env: Env, root: Path, bin_dir: Path, sudo_user: str | None
) -> None:
    result = setup(host(root, bin_dir, sudo_user=sudo_user))

    assert result.exit_code == 1
    assert "run it with sudo from your own account" in result.output
    assert_nothing_installed(root)


def test_setup_takes_no_set(env: Env, root: Path, bin_dir: Path) -> None:
    result = invoke(["jira-123", "setup"], FakeGit(), host=host(root, bin_dir))

    assert result.exit_code == 2
    assert "setup takes no set" in result.output


# --- the mkcert CA -------------------------------------------------------------------------


def test_setup_runs_mkcert_install_as_the_sudo_user_never_as_root(
    env: Env, root: Path, bin_dir: Path
) -> None:
    runner = FakeRunner()

    result = setup(host(root, bin_dir, runner=runner))

    assert result.exit_code == 0, result.output
    mkcert = str(bin_dir / "mkcert")
    assert [call for call in runner.calls if mkcert in call] == [
        ["sudo", "-u", "dev", "-H", "--", mkcert, "-install"]
    ]


def test_setup_fails_before_installing_anything_without_mkcert(
    env: Env, root: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    result = setup(host(root, empty))

    assert result.exit_code == 1
    assert "mkcert is not installed or not on PATH" in result.output
    assert_nothing_installed(root)


def test_a_failing_mkcert_install_fails_setup(env: Env, root: Path, bin_dir: Path) -> None:
    runner = FakeRunner()
    runner.answer(["sudo", "-u", "dev"], 1, "ERROR: no trust store")

    result = setup(host(root, bin_dir, runner=runner))

    assert result.exit_code == 1
    assert "ERROR: no trust store" in result.output


# --- nothing of the loopback model is installed ----------------------------------------------


def test_setup_has_no_sets_option(env: Env, root: Path, bin_dir: Path) -> None:
    runner = FakeRunner()

    result = setup(host(root, bin_dir, runner=runner), "--sets", "2")

    assert result.exit_code == 2
    assert "No such option: --sets" in result.output
    assert runner.calls == []


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_setup_installs_no_helper_no_sudoers_rule_and_no_alias_job(
    env: Env, root: Path, bin_dir: Path, platform: str
) -> None:
    runner = FakeRunner()

    result = setup(host(root, bin_dir, platform=platform, runner=runner))

    assert result.exit_code == 0, result.output
    assert_nothing_installed(root)
    for program in ("visudo", "ifconfig", "ip", "launchctl", "/bin/sh"):
        assert runner.ran(program) == [], program
    assert (root / HOSTS).read_text() == OWN_HOSTS


# --- what an upgraded machine still carries --------------------------------------------------

BLOCKS = (
    "# sandbox:jira-123\n127.42.0.1\tmysql.jira-123.test\n127.42.0.2\tweb.jira-123.test\n"
    "# end sandbox:jira-123\n",
    "# sandbox:other\n127.42.1.1\tmysql.other.test\n# end sandbox:other\n",
)


def old_alias_job(*prefixes: str) -> bytes:
    """The launchd job as the loopback model wrote it, aliasing each ``a.b.c`` /24."""
    script = (
        f"for p in {' '.join(prefixes)} ; do h=1; while [ $h -le 254 ]; do "
        '/sbin/ifconfig lo0 alias "$p.$h" up; h=$((h + 1)); done; done'
    )
    return plistlib.dumps(
        {"Label": LABEL, "ProgramArguments": ["/bin/sh", "-c", script], "RunAtLoad": True}
    )


def leave_loopback_installation(root: Path, *, alias_job: bytes | None = None) -> None:
    helper = root / HELPER
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/usr/bin/python3 -IS\n")
    helper.chmod(0o755)
    package = root / BUNDLE / "sandbox"
    package.mkdir(parents=True)
    (package / "hosts_helper.py").write_text("")
    sudoers = root / SUDOERS
    sudoers.parent.mkdir(parents=True)
    sudoers.write_text("dev ALL=(root) NOPASSWD: /usr/local/bin/sandbox-hosts\n")
    (root / HOSTS).write_text(
        "127.0.0.1\tlocalhost\n" + BLOCKS[0] + "::1 ip6-localhost\n" + BLOCKS[1]
    )
    if alias_job is not None:
        (root / PLIST).parent.mkdir(parents=True)
        (root / PLIST).write_bytes(alias_job)


def run_alias_script(script: str, tmp_path: Path) -> list[str]:
    """Run a generated script with a fake ifconfig; the argument lists it was called with."""
    log = tmp_path / "ifconfig.log"
    fake = tmp_path / "ifconfig"
    fake.write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
    fake.chmod(0o755)
    subprocess.run(["/bin/sh", "-c", script.replace("/sbin/ifconfig", str(fake))], check=True)
    return log.read_text().splitlines() if log.exists() else []


DIRECTIONS = pytest.mark.parametrize("args", [(), ("--uninstall",)], ids=["setup", "uninstall"])


@DIRECTIONS
def test_the_hosts_helper_its_bundle_and_its_sudoers_rule_are_removed(
    env: Env, root: Path, bin_dir: Path, args: tuple[str, ...]
) -> None:
    leave_loopback_installation(root)

    result = setup(host(root, bin_dir), *args)

    assert result.exit_code == 0, result.output
    assert_nothing_installed(root)
    for path in ("/usr/local/bin/sandbox-hosts", "/usr/local/lib/sandbox-hosts/",
                 "/etc/sudoers.d/sandbox-hosts"):  # fmt: skip
        assert f"removed {path} " in result.output, path


@DIRECTIONS
def test_leftover_hosts_blocks_are_removed_and_every_other_line_is_kept(
    env: Env, root: Path, bin_dir: Path, args: tuple[str, ...]
) -> None:
    leave_loopback_installation(root)
    (root / HOSTS).chmod(0o644)

    result = setup(host(root, bin_dir), *args)

    assert result.exit_code == 0, result.output
    assert (root / HOSTS).read_text() == OWN_HOSTS
    assert mode(root / HOSTS) == 0o644
    assert "removed 2 `# sandbox:` blocks from /etc/hosts" in result.output


@DIRECTIONS
def test_a_hosts_block_without_its_end_line_is_left_for_the_developer(
    env: Env, root: Path, bin_dir: Path, args: tuple[str, ...]
) -> None:
    """Everything after an unterminated block could be the developer's own lines."""
    broken = "# sandbox:broken\n127.42.2.1\tmysql.broken.test\n10.0.0.5\tnas.home\n"
    (root / HOSTS).write_text(OWN_HOSTS + BLOCKS[1] + broken)

    result = setup(host(root, bin_dir), *args)

    assert result.exit_code == 0, result.output
    assert (root / HOSTS).read_text() == OWN_HOSTS + broken
    assert "removed 1 `# sandbox:` block from /etc/hosts" in result.output
    assert "`# sandbox:broken` in /etc/hosts has no `# end sandbox:broken` line" in result.output


@DIRECTIONS
def test_on_macos_the_alias_job_and_its_aliases_are_removed(
    env: Env, root: Path, bin_dir: Path, tmp_path: Path, args: tuple[str, ...]
) -> None:
    leave_loopback_installation(root, alias_job=old_alias_job("127.42.0", "127.42.1"))
    runner = FakeRunner()

    result = setup(host(root, bin_dir, platform="darwin", runner=runner), *args)

    assert result.exit_code == 0, result.output
    assert_nothing_installed(root)
    assert ["launchctl", "bootout", f"system/{LABEL}"] in runner.calls
    (removal,) = runner.ran("/bin/sh", "-c")
    calls = run_alias_script(removal[2], tmp_path)
    assert len(calls) == 2 * 254
    assert calls[0] == "lo0 -alias 127.42.0.1"
    assert calls[-1] == "lo0 -alias 127.42.1.254"
    assert "removed /Library/LaunchDaemons/sandbox.loopback-aliases.plist " in result.output
    assert "removed the lo0 aliases of 2 /24 blocks" in result.output


@pytest.mark.parametrize(
    "job",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not a plist", id="not-a-plist"),
        pytest.param(plistlib.dumps({"Label": LABEL}), id="no-program"),
        pytest.param(
            plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/bin/sh", "-c", "echo hi"]}),
            id="another-script",
        ),
        # Not loopback /24s: never handed to a root shell.
        pytest.param(old_alias_job("10.0.0", "127.42.0"), id="not-loopback"),
        pytest.param(old_alias_job("127.42.0;reboot"), id="shell-separator"),
        pytest.param(old_alias_job("$(reboot)"), id="shell-substitution"),
        pytest.param(
            old_alias_job("127.42.0", "127.42.1").replace(b"127.42.1", b"127.42.999"),
            id="not-an-address",
        ),
    ],
)
def test_an_alias_job_setup_cannot_read_is_removed_without_running_anything_from_it(
    env: Env, root: Path, bin_dir: Path, job: bytes
) -> None:
    leave_loopback_installation(root, alias_job=job)
    runner = FakeRunner()

    result = setup(host(root, bin_dir, platform="darwin", runner=runner))

    assert result.exit_code == 0, result.output
    assert_nothing_installed(root)
    assert ["launchctl", "bootout", f"system/{LABEL}"] in runner.calls
    assert runner.ran("/bin/sh") == []
    assert "restart" in result.output


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@DIRECTIONS
def test_a_machine_with_nothing_left_over_is_not_touched(
    env: Env, root: Path, bin_dir: Path, platform: str, args: tuple[str, ...]
) -> None:
    runner = FakeRunner()
    before = (root / HOSTS).stat().st_mtime_ns

    result = setup(host(root, bin_dir, platform=platform, runner=runner), *args)

    assert result.exit_code == 0, result.output
    assert (root / HOSTS).read_text() == OWN_HOSTS
    assert (root / HOSTS).stat().st_mtime_ns == before
    assert runner.ran("launchctl") == []
    assert runner.ran("/bin/sh") == []
    assert "sandbox-hosts" not in result.output
    assert "/etc/hosts" not in result.output


def test_a_machine_without_a_hosts_file_is_fine(env: Env, root: Path, bin_dir: Path) -> None:
    (root / HOSTS).unlink()

    result = setup(host(root, bin_dir))

    assert result.exit_code == 0, result.output
    assert not (root / HOSTS).exists()


def test_a_failed_setup_removes_nothing(env: Env, root: Path, tmp_path: Path) -> None:
    """No mkcert: setup refuses before it changes the machine in either direction."""
    leave_loopback_installation(root)
    before = (root / HOSTS).read_text()
    empty = tmp_path / "empty"
    empty.mkdir()

    result = setup(host(root, empty))

    assert result.exit_code == 1
    assert (root / HELPER).exists()
    assert (root / SUDOERS).exists()
    assert (root / HOSTS).read_text() == before


# --- uninstall -----------------------------------------------------------------------------


def test_uninstall_keeps_the_ca(env: Env, root: Path, bin_dir: Path) -> None:
    runner = FakeRunner()
    assert setup(host(root, bin_dir, runner=runner)).exit_code == 0
    runner.calls.clear()

    result = setup(host(root, bin_dir, runner=runner), "--uninstall")

    assert result.exit_code == 0, result.output
    assert [call for call in runner.calls if call[0] != "systemctl"] == []
    assert "mkcert -uninstall" in result.output


def test_uninstall_when_nothing_is_installed_succeeds(env: Env, root: Path, bin_dir: Path) -> None:
    result = setup(host(root, bin_dir), "--uninstall")

    assert result.exit_code == 0, result.output


def test_uninstall_does_not_need_a_valid_config(env: Env, root: Path, bin_dir: Path) -> None:
    """A config still carrying a key of the loopback model must not block the clean-up."""
    leave_loopback_installation(root, alias_job=old_alias_job("127.50.0"))
    config = root / "home" / "dev" / ".config" / "sandbox" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("ip_pool: 127.50.0.0/16\n")
    env.config_file.write_text("ip_pool: 127.50.0.0/16\n")

    result = setup(host(root, bin_dir, platform="darwin"), "--uninstall")

    assert result.exit_code == 0, result.output
    assert_nothing_installed(root)
