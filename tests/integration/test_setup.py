"""`sandbox setup` with real commands, installing into a temp root.

The real install into ``/`` (`mkcert -install`, pointing the real systemd-resolved at the
resolver) is covered by the end-to-end suite on a sudo runner. Here setup runs against a
machine that still carries what an earlier version installed, and removes it for real. On
macOS (as root) the `lo0` aliases that version added are real ones, and must be gone
afterwards; that path needs a Mac with sudo and is otherwise verified by hand.
"""

import asyncio
import os
import plistlib
import re
import sys
from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from sandbox.platforms import Host, run_command
from sandbox.resolver_config import installed_domain
from tests.conftest import Env, invoke
from tests.fakes import FakeGit

pytestmark = pytest.mark.integration

HELPER = "usr/local/bin/sandbox-hosts"
BUNDLE = "usr/local/lib/sandbox-hosts"
SUDOERS = "etc/sudoers.d/sandbox-hosts"
PLIST = "Library/LaunchDaemons/sandbox.loopback-aliases.plist"
OWN_HOSTS = "127.0.0.1\tlocalhost\n"
# The last /24 of the range the earlier version aliased by default: no set ever got that far.
ALIASED = IPv4Network("127.42.255.0/24")


class MostlyRealRunner:
    """Runs every command for real except `sudo` (mkcert -install as the user), `launchctl`
    and `systemctl` (the machine's own launchd and systemd-resolved are never touched):
    those are recorded, and succeed."""

    def __init__(self) -> None:
        self.faked: list[list[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        if argv[0] in ("sudo", "launchctl", "systemctl"):
            self.faked.append(list(argv))
            return 0, ""
        return await run_command(argv)


def make_host(env: Env, root: Path, platform: str) -> tuple[Host, MostlyRealRunner]:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mkcert").write_text("#!/bin/sh\n")
    (bin_dir / "mkcert").chmod(0o755)
    runner = MostlyRealRunner()
    host = Host(
        platform=platform,
        root=root,
        run=runner,
        euid=0,
        env={
            "SUDO_USER": "dev",
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "SANDBOX_CONFIG": str(env.config_file),
        },
    )
    return host, runner


def leave_earlier_installation(root: Path) -> None:
    for relative, text in (
        (HELPER, "#!/usr/bin/python3 -IS\n"),
        (f"{BUNDLE}/sandbox/hosts_helper.py", ""),
        (SUDOERS, "dev ALL=(root) NOPASSWD: /usr/local/bin/sandbox-hosts\n"),
        (
            "etc/hosts",
            OWN_HOSTS + "# sandbox:old\n127.42.0.1\tmysql.old.test\n# end sandbox:old\n",
        ),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def assert_earlier_installation_gone(root: Path) -> None:
    for relative in (HELPER, BUNDLE, SUDOERS, PLIST):
        assert not (root / relative).exists(), relative
    assert (root / "etc" / "hosts").read_text() == OWN_HOSTS


@pytest.mark.skipif(sys.platform != "linux", reason="Linux path")
def test_on_linux_setup_points_at_the_resolver_and_removes_an_earlier_installation(
    env: Env, tmp_path: Path
) -> None:
    root = tmp_path / "machine"
    host, runner = make_host(env, root, "linux")
    leave_earlier_installation(root)

    result = invoke(["setup"], FakeGit(), host=host)

    assert result.exit_code == 0, result.output
    assert_earlier_installation_gone(root)
    assert installed_domain(host) == "test"
    assert [call for call in runner.faked if call[0] == "sudo"] == [
        ["sudo", "-u", "dev", "-H", "--", str(root / "bin" / "mkcert"), "-install"]
    ]
    assert ["systemctl", "reload-or-restart", "systemd-resolved.service"] in runner.faked


@pytest.mark.skipif(
    sys.platform != "darwin" or os.geteuid() != 0,
    reason="macOS lo0 path: needs a Mac and root (sudo); verified by hand otherwise",
)
def test_on_macos_setup_removes_the_alias_job_and_its_aliases(env: Env, tmp_path: Path) -> None:
    root = tmp_path / "machine"
    host, runner = make_host(env, root, "darwin")
    leave_earlier_installation(root)
    prefix = str(ALIASED.network_address).rsplit(".", 1)[0]
    script = (
        f"for p in {prefix} ; do h=1; while [ $h -le 254 ]; do "
        '/sbin/ifconfig lo0 alias "$p.$h" up; h=$((h + 1)); done; done'
    )
    job = root / PLIST
    job.parent.mkdir(parents=True)
    job.write_bytes(
        plistlib.dumps(
            {
                "Label": "sandbox.loopback-aliases",
                "ProgramArguments": ["/bin/sh", "-c", script],
                "RunAtLoad": True,
            }
        )
    )
    try:
        status, output = asyncio.run(run_command(["/bin/sh", "-c", script]))
        assert status == 0, output
        assert all(ip in _aliases() for ip in ALIASED.hosts())

        result = invoke(["setup"], FakeGit(), host=host)

        assert result.exit_code == 0, result.output
        assert_earlier_installation_gone(root)
        assert ["launchctl", "bootout", "system/sandbox.loopback-aliases"] in runner.faked
        assert not any(ip in ALIASED for ip in _aliases())
    finally:
        asyncio.run(run_command(["/bin/sh", "-c", script.replace(" alias ", " -alias ")]))


def _aliases() -> set[IPv4Address]:
    """The IPv4 addresses on ``lo0`` right now."""
    status, output = asyncio.run(run_command(["ifconfig", "lo0"]))
    assert status == 0, output
    return {IPv4Address(ip) for ip in re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", output)}
