"""`sandbox setup` against the real system tools, installing into a temp root.

The real install into ``/`` (helper, sudoers rule, `mkcert -install`, `/etc/hosts`) is
covered by the end-to-end suite on a sudo runner. Here the commands that only *read* the
machine or check a file run for real: `visudo -c` on the generated rule, `ip`/`ifconfig`
for the addresses in use. On macOS (as root) the generated alias script runs for real and
`up`'s preflight must then see the pool; that path needs a Mac with sudo and is otherwise
verified by hand (see the README's setup section).
"""

import asyncio
import os
import shutil
import sys
from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from sandbox.platforms import Host, run_command
from sandbox.preflight import aliased_blocks, aliased_ips
from sandbox.setup import HELPER_PATH, LAUNCHD_PLIST, SUDOERS_PATH
from tests.conftest import Env, invoke
from tests.fakes import FakeGit

pytestmark = pytest.mark.integration


class MostlyRealRunner:
    """Runs every command for real except `sudo` (mkcert -install as the user) and
    `launchctl` (the machine's own launchd job is never touched): those are recorded."""

    def __init__(self) -> None:
        self.faked: list[list[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        if argv[0] in ("sudo", "launchctl"):
            self.faked.append(list(argv))
            return 0, ""
        return await run_command(argv)


def make_host(env: Env, root: Path, platform: str) -> tuple[Host, MostlyRealRunner]:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mkcert").write_text("#!/bin/sh\n")
    (bin_dir / "mkcert").chmod(0o755)
    (root / "etc").mkdir()
    (root / "etc" / "hosts").write_text("127.0.0.1\tlocalhost\n")
    runner = MostlyRealRunner()
    host = Host(
        platform=platform,
        root=root,
        run=runner,
        euid=0,
        env={
            "SUDO_USER": "dev",
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            # The test's own /20, never the default pool real sets use.
            "SANDBOX_CONFIG": str(env.config_file),
        },
        python=Path(sys.executable),
    )
    return host, runner


@pytest.mark.skipif(sys.platform != "linux", reason="Linux path")
@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo is not installed")
@pytest.mark.skipif(shutil.which("ip") is None, reason="iproute2 is not installed")
def test_on_linux_setup_installs_a_rule_visudo_accepts(env: Env, tmp_path: Path) -> None:
    root = tmp_path / "machine"
    host, runner = make_host(env, root, "linux")

    result = invoke(["setup"], FakeGit(), host=host)

    assert result.exit_code == 0, result.output
    assert host.path(SUDOERS_PATH).is_file()
    assert host.path(HELPER_PATH).is_file()
    assert not host.path(LAUNCHD_PLIST).exists()
    assert runner.faked == [["sudo", "-u", "dev", "-H", "--", str(root / "bin" / "mkcert"),
                             "-install"]]  # fmt: skip


@pytest.mark.skipif(
    sys.platform != "darwin" or os.geteuid() != 0,
    reason="macOS launchd/lo0 path: needs a Mac and root (sudo); verified by hand otherwise",
)
def test_on_macos_setup_aliases_the_pool_and_uninstall_removes_it(
    env: Env, tmp_path: Path, ip_pool: IPv4Network
) -> None:
    root = tmp_path / "machine"
    host, _ = make_host(env, root, "darwin")

    installed = invoke(["setup", "--sets", "2"], FakeGit(), host=host)
    try:
        assert installed.exit_code == 0, installed.output
        assert host.path(LAUNCHD_PLIST).is_file()
        live = aliased_blocks(ip_pool, _aliases(host))
        assert live == 2
    finally:
        removed = invoke(["setup", "--uninstall"], FakeGit(), host=host)
    assert removed.exit_code == 0, removed.output
    assert aliased_blocks(ip_pool, _aliases(host)) == 0


def _aliases(host: Host) -> set[IPv4Address]:
    return asyncio.run(aliased_ips(host))
