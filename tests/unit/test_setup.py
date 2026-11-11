"""`sudo sandbox setup [--uninstall] [--sets N]` installs what sandbox needs root for.

Driven through the CLI seam with a fake host (``tests.fakes_host``): it is "root", its
filesystem root is a temp directory, and every command it would run (`visudo`, `ip`,
`ifconfig`, `launchctl`, `sudo -u ... mkcert`) is recorded and answered from a table. The
installed helper bundle is real: tests run it under a real Python with ``-I -S``.
"""

import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox.platforms import Host
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_host import FakeRunner, lo0_with_aliases, make_host

HELPER = "usr/local/bin/sandbox-hosts"
BUNDLE = "usr/local/lib/sandbox-hosts"
SUDOERS = "etc/sudoers.d/sandbox-hosts"
PLIST = "Library/LaunchDaemons/sandbox.loopback-aliases.plist"
LABEL = "sandbox.loopback-aliases"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "machine"
    (path / "etc").mkdir(parents=True)
    (path / "etc" / "hosts").write_text("127.0.0.1\tlocalhost\n")
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


def plist(root: Path) -> dict[str, Any]:
    return cast(dict[str, Any], plistlib.loads((root / PLIST).read_bytes()))


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


# --- the hosts helper and its sudoers rule -------------------------------------------------


def test_setup_installs_the_hosts_helper_root_owned_and_executable(
    env: Env, root: Path, bin_dir: Path
) -> None:
    result = setup(host(root, bin_dir))

    assert result.exit_code == 0, result.output
    helper = root / HELPER
    assert mode(helper) == 0o755
    assert helper.read_text().splitlines()[0] == f"#!{sys.executable} -IS"
    assert mode(root / BUNDLE / "sandbox" / "hosts_helper.py") == 0o644
    # Runnable as installed: the shebang's Python, no site-packages, nothing outside the bundle.
    usage = subprocess.run([helper, "--help"], capture_output=True, text=True, check=False)
    assert usage.returncode == 0, usage.stderr
    assert "sandbox-hosts" in usage.stdout


def run_bundle(
    python: str, root: Path, hosts: Path, *argv: str
) -> subprocess.CompletedProcess[str]:
    """Run the installed bundle's helper on ``hosts`` (the installed one only edits /etc/hosts)."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(root / BUNDLE)!r})\n"
        "from pathlib import Path\n"
        "from sandbox.hosts_helper import run\n"
        f"status = run({list(argv)!r}, hosts_path=Path({str(hosts)!r}),"
        f" lock_path=Path({str(hosts) + '.lock'!r}))\n"
        "third_party = {'pydantic', 'yaml', 'typer', 'click'} & set(sys.modules)\n"
        "assert not third_party, third_party\n"
        "sys.exit(status)\n"
    )
    return subprocess.run(
        [python, "-I", "-S", "-c", code], capture_output=True, text=True, check=False
    )


def _pythons() -> list[str]:
    """This Python, the system one the installed helper runs under (3.9 on macOS's Xcode
    command line tools), and any python3.9 on PATH."""
    found = [sys.executable, "/usr/bin/python3", shutil.which("python3.9")]
    real = [p for p in found if p is not None and Path(p).is_file()]
    return list(dict.fromkeys(real))


@pytest.mark.parametrize("python", _pythons())
def test_the_installed_helper_bundle_works_with_the_standard_library_only(
    env: Env, root: Path, bin_dir: Path, tmp_path: Path, python: str
) -> None:
    assert setup(host(root, bin_dir)).exit_code == 0
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n")

    written = run_bundle(
        python, root, hosts, "write", "jira-123", "127.42.0.0/24", "mysql.jira-123.test=127.42.0.1"
    )
    refused = run_bundle(python, root, hosts, "write", "jira-123", "127.42.0.0/24", "a.com=1.2.3.4")

    assert written.returncode == 0, written.stderr
    assert "127.42.0.1\tmysql.jira-123.test\n" in hosts.read_text()
    assert refused.returncode == 1
    assert "does not end in .test" in refused.stderr


def test_setup_scopes_a_nopasswd_sudoers_rule_to_the_helper_only(
    env: Env, root: Path, bin_dir: Path
) -> None:
    runner = FakeRunner()

    result = setup(host(root, bin_dir, runner=runner))

    assert result.exit_code == 0, result.output
    sudoers = root / SUDOERS
    rules = [line for line in sudoers.read_text().splitlines() if not line.startswith("#")]
    assert rules == ["dev ALL=(root) NOPASSWD: /usr/local/bin/sandbox-hosts"]
    assert mode(sudoers) == 0o440
    (visudo,) = runner.ran("visudo")
    assert visudo[:2] == ["visudo", "-cf"]


def test_a_sudoers_rule_visudo_rejects_is_not_installed(
    env: Env, root: Path, bin_dir: Path
) -> None:
    runner = FakeRunner()
    runner.answer(["visudo"], 1, "parse error")

    result = setup(host(root, bin_dir, runner=runner))

    assert result.exit_code == 1
    assert "parse error" in result.output
    assert list((root / SUDOERS).parent.iterdir()) == []


def test_rerunning_setup_replaces_the_installation(env: Env, root: Path, bin_dir: Path) -> None:
    assert setup(host(root, bin_dir)).exit_code == 0
    stale = root / BUNDLE / "sandbox" / "stale.py"
    stale.write_text("")

    result = setup(host(root, bin_dir))

    assert result.exit_code == 0, result.output
    assert not stale.exists()


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


# --- the IP range ----------------------------------------------------------------------------


def test_on_linux_setup_performs_no_loopback_setup(env: Env, root: Path, bin_dir: Path) -> None:
    runner = FakeRunner()

    result = setup(host(root, bin_dir, runner=runner))

    assert result.exit_code == 0, result.output
    assert not (root / PLIST).exists()
    assert runner.ran("ifconfig") == []
    assert runner.ran("launchctl") == []
    assert "no loopback setup needed on Linux" in result.output


def test_setup_refuses_when_an_interface_has_an_address_in_the_ip_range(
    env: Env, root: Path, bin_dir: Path
) -> None:
    runner = FakeRunner()
    runner.answer(
        ["ip", "-4", "-o", "addr", "show"],
        0,
        "1: lo    inet 127.0.0.1/8 scope host lo\n1: lo    inet 127.42.3.7/32 scope host lo\n",
    )

    result = setup(host(root, bin_dir, runner=runner))

    assert result.exit_code == 1
    assert "127.42.0.0/16 is already in use" in result.output
    assert "127.42.3.7 (lo)" in result.output
    assert_nothing_installed(root)
    assert runner.ran("sudo") == []


def test_setup_refuses_when_the_hosts_file_maps_a_name_into_the_ip_range(
    env: Env, root: Path, bin_dir: Path
) -> None:
    (root / "etc" / "hosts").write_text(
        "127.0.0.1\tlocalhost\n127.42.0.9 db.local\n"
        "# sandbox:jira-123\n127.42.0.1\tmysql.jira-123.test\n# end sandbox:jira-123\n"
    )

    result = setup(host(root, bin_dir))

    assert result.exit_code == 1
    assert "127.42.0.9 (/etc/hosts: db.local)" in result.output
    assert "mysql.jira-123.test" not in result.output  # sandbox's own blocks are fine
    assert_nothing_installed(root)


def test_setup_uses_the_invoking_users_config(env: Env, root: Path, bin_dir: Path) -> None:
    config = root / "home" / "dev" / ".config" / "sandbox" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("ip_pool: 127.50.0.0/16\n")
    runner = FakeRunner()
    runner.answer(
        ["ip", "-4", "-o", "addr", "show"], 0, "1: lo    inet 127.42.3.7/32 scope host lo"
    )

    result = setup(host(root, bin_dir, runner=runner))

    assert result.exit_code == 0, result.output  # 127.42.3.7 is outside 127.50.0.0/16


# --- the macOS loopback alias pool ---------------------------------------------------------


def macos(root: Path, bin_dir: Path, lo0: str = "") -> tuple[Host, FakeRunner]:
    runner = FakeRunner()
    runner.answer(["ifconfig", "lo0"], 0, lo0 or lo0_with_aliases())
    runner.answer(["ifconfig", "-a"], 0, lo0 or lo0_with_aliases())
    return host(root, bin_dir, platform="darwin", runner=runner), runner


def alias_script(root: Path) -> str:
    program = plist(root)["ProgramArguments"]
    assert program[:2] == ["/bin/sh", "-c"]
    return program[2]


def run_alias_script(script: str, tmp_path: Path) -> list[str]:
    """Run a generated script with a fake ifconfig; the argument lists it was called with."""
    log = tmp_path / "ifconfig.log"
    fake = tmp_path / "ifconfig"
    fake.write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
    fake.chmod(0o755)
    subprocess.run(["/bin/sh", "-c", script.replace("/sbin/ifconfig", str(fake))], check=True)
    return log.read_text().splitlines() if log.exists() else []


def test_on_macos_setup_installs_a_launchd_job_aliasing_16_blocks_at_boot(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, runner = macos(root, bin_dir)

    result = setup(h)

    assert result.exit_code == 0, result.output
    job = plist(root)
    assert job["Label"] == LABEL
    assert job["RunAtLoad"] is True
    assert mode(root / PLIST) == 0o644
    script = alias_script(root)
    assert "127.42.0 " in script
    assert "127.42.15" in script
    assert "127.42.16" not in script
    # The pool is live now, not only after the next boot.
    assert ["/bin/sh", "-c", script] in runner.calls
    assert "16 /24 blocks (127.42.0.0/24 to 127.42.15.0/24)" in result.output


def test_the_launchd_script_aliases_every_host_address_of_each_block(
    env: Env, root: Path, bin_dir: Path, tmp_path: Path
) -> None:
    h, _ = macos(root, bin_dir)
    assert setup(h, "--sets", "2").exit_code == 0

    calls = run_alias_script(alias_script(root), tmp_path)

    assert len(calls) == 2 * 254
    assert calls[0] == "lo0 alias 127.42.0.1 up"
    assert calls[253] == "lo0 alias 127.42.0.254 up"
    assert calls[-1] == "lo0 alias 127.42.1.254 up"


def test_rerunning_setup_with_fewer_sets_drops_the_extra_aliases(
    env: Env, root: Path, bin_dir: Path, tmp_path: Path
) -> None:
    lo0 = lo0_with_aliases("127.42.0.0/24", "127.42.1.0/24", "127.42.2.0/24")
    h, runner = macos(root, bin_dir, lo0)
    (root / PLIST).parent.mkdir(parents=True)
    (root / PLIST).write_bytes(b"")  # an earlier setup's job: its aliases are not "in use"

    result = setup(h, "--sets", "1")

    assert result.exit_code == 0, result.output
    removals = [call[2] for call in runner.ran("/bin/sh", "-c") if "-alias" in call[2]]
    assert len(removals) == 1
    calls = run_alias_script(removals[0], tmp_path)
    assert len(calls) == 2 * 254
    assert calls[0] == "lo0 -alias 127.42.1.1"


def test_on_macos_setup_refuses_addresses_in_the_range_it_did_not_alias(
    env: Env, root: Path, bin_dir: Path
) -> None:
    h, _ = macos(root, bin_dir, lo0_with_aliases("127.42.0.0/24"))

    result = setup(h)

    assert result.exit_code == 1
    assert "127.42.0.1/24" not in result.output
    assert "127.42.0.1 (lo0)" in result.output
    assert_nothing_installed(root)


@pytest.mark.parametrize(("sets", "message"), [("0", "at least 1"), ("257", "at most 256")])
def test_sets_must_fit_the_ip_pool(
    env: Env, root: Path, bin_dir: Path, sets: str, message: str
) -> None:
    h, _ = macos(root, bin_dir)

    result = setup(h, "--sets", sets)

    assert result.exit_code != 0
    assert message in result.output
    assert_nothing_installed(root)


# --- uninstall -----------------------------------------------------------------------------


def test_uninstall_removes_helper_sudoers_and_launchd_job_but_keeps_the_ca(
    env: Env, root: Path, bin_dir: Path, tmp_path: Path
) -> None:
    h, runner = macos(root, bin_dir)
    assert setup(h).exit_code == 0
    runner.calls.clear()
    runner.answer(["ifconfig", "lo0"], 0, lo0_with_aliases("127.42.0.0/24"))

    result = setup(h, "--uninstall")

    assert result.exit_code == 0, result.output
    assert_nothing_installed(root)
    assert ["launchctl", "bootout", f"system/{LABEL}"] in runner.calls
    (removal,) = runner.ran("/bin/sh", "-c")
    assert run_alias_script(removal[2], tmp_path)[0] == "lo0 -alias 127.42.0.1"
    assert not [call for call in runner.calls if any("mkcert" in arg for arg in call)]
    assert "mkcert -uninstall" in result.output


def test_uninstall_on_linux_needs_no_loopback_teardown(env: Env, root: Path, bin_dir: Path) -> None:
    runner = FakeRunner()
    assert setup(host(root, bin_dir, runner=runner)).exit_code == 0
    runner.calls.clear()

    result = setup(host(root, bin_dir, runner=runner), "--uninstall")

    assert result.exit_code == 0, result.output
    assert_nothing_installed(root)
    assert runner.calls == []
    assert "mkcert -uninstall" in result.output


def test_uninstall_when_nothing_is_installed_succeeds(env: Env, root: Path, bin_dir: Path) -> None:
    result = setup(host(root, bin_dir), "--uninstall")

    assert result.exit_code == 0, result.output


def test_the_helper_bundles_constants_mirror_sandboxs_own() -> None:
    from sandbox import config, hosts_block, leases
    from sandbox.compose import network

    assert hosts_block.LOOPBACK == config.LOOPBACK
    assert hosts_block.BLOCK_PREFIX == leases.BLOCK_PREFIX
    assert hosts_block.TLD == network.TLD
