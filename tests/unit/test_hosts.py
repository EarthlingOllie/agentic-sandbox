"""Hosts writers: production goes through `sudo -n sandbox-hosts`; writes are flock-guarded."""

import asyncio
import subprocess
import sys
from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from sandbox.config import Config
from sandbox.errors import SandboxError
from sandbox.hosts import DirectHostsWriter, HelperHostsWriter, hosts_writer
from sandbox.hosts_helper import run as run_helper

OTHER = "127.0.0.1\tlocalhost\n"
BLOCK = IPv4Network("127.42.0.0/24")
ENTRIES = {"mysql.jira-123.test": IPv4Address("127.42.0.1")}


class SudoHelper:
    """Stands in for `sudo -n`: records argv, then runs the helper on a temp hosts file."""

    def __init__(self, hosts: Path, status: int | None = None, output: str = "") -> None:
        self.hosts = hosts
        self.status = status
        self.output = output
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        if self.status is not None:
            return self.status, self.output
        assert argv[:3] == ["sudo", "-n", "/usr/local/bin/sandbox-hosts"]
        lock = self.hosts.with_name("hosts.lock")
        # In a thread, as the real helper runs in its own process (with its own event loop).
        status = await asyncio.to_thread(
            run_helper, argv[3:], hosts_path=self.hosts, lock_path=lock
        )
        return status, ""


@pytest.fixture
def hosts(tmp_path: Path) -> Path:
    path = tmp_path / "hosts"
    path.write_text(OTHER)
    return path


def test_production_config_writes_through_the_helper() -> None:
    assert isinstance(hosts_writer(Config()), HelperHostsWriter)


def test_a_configured_hosts_file_is_written_directly(tmp_path: Path) -> None:
    config = Config(state_dir=tmp_path, hosts_file=tmp_path / "hosts")
    assert isinstance(hosts_writer(config), DirectHostsWriter)


def test_helper_writer_runs_sandbox_hosts_non_interactively(hosts: Path) -> None:
    sudo = SudoHelper(hosts)
    writer = HelperHostsWriter(sudo, path=hosts)

    asyncio.run(writer.write("jira-123", BLOCK, ENTRIES))
    asyncio.run(writer.remove("jira-123"))

    assert sudo.calls == [
        ["sudo", "-n", "/usr/local/bin/sandbox-hosts", "write", "jira-123", "127.42.0.0/24",
         "mysql.jira-123.test=127.42.0.1"],
        ["sudo", "-n", "/usr/local/bin/sandbox-hosts", "remove", "jira-123"],
    ]  # fmt: skip
    assert hosts.read_text() == OTHER


def test_helper_writer_skips_sudo_when_nothing_would_change(hosts: Path) -> None:
    sudo = SudoHelper(hosts)
    writer = HelperHostsWriter(sudo, path=hosts)

    asyncio.run(writer.remove("jira-123"))  # no block yet
    asyncio.run(writer.write("jira-123", BLOCK, ENTRIES))
    asyncio.run(writer.write("jira-123", BLOCK, ENTRIES))  # already there

    assert [call[3] for call in sudo.calls] == ["write"]


def test_a_failing_sudo_points_at_setup(hosts: Path) -> None:
    sudo = SudoHelper(hosts, status=1, output="sudo: a password is required")
    writer = HelperHostsWriter(sudo, path=hosts)

    with pytest.raises(SandboxError, match="a password is required") as caught:
        asyncio.run(writer.write("jira-123", BLOCK, ENTRIES))

    assert "sudo sandbox setup" in str(caught.value)


def test_helper_writer_refuses_bad_entries_before_running_sudo(hosts: Path) -> None:
    sudo = SudoHelper(hosts)
    writer = HelperHostsWriter(sudo, path=hosts)

    with pytest.raises(SandboxError, match=r"does not end in \.test"):
        asyncio.run(writer.write("jira-123", BLOCK, {"github.com": IPv4Address("127.42.0.1")}))

    assert sudo.calls == []


WRITER = """
import sys
from pathlib import Path
from sandbox.hosts_helper import run
hosts, set_name, third = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
for i in range(1, 21):
    entry = f"svc{i}.{set_name}.test=127.42.{third}.{i}"
    status = run(["write", set_name, f"127.42.{third}.0/24", entry],
                 hosts_path=hosts, lock_path=hosts.with_name("hosts.lock"))
    assert status == 0
"""


def test_concurrent_processes_never_lose_each_others_blocks(hosts: Path) -> None:
    processes = [
        subprocess.Popen([sys.executable, "-c", WRITER, str(hosts), f"set-{n}", str(n)])
        for n in range(8)
    ]
    assert [process.wait(timeout=60) for process in processes] == [0] * 8

    text = hosts.read_text()
    assert text.startswith(OTHER)
    for n in range(8):
        assert (
            f"# sandbox:set-{n}\n127.42.{n}.20\tsvc20.set-{n}.test\n# end sandbox:set-{n}\n" in text
        )
    assert text.count("# sandbox:") == 8
