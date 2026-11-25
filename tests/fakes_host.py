"""A stand-in for the machine sandbox runs on (``sandbox.platforms.Host``), for unit tests.

Its filesystem root is a temp directory, so ``setup`` installs the helper, the sudoers rule
and the launchd job under it, and every command (``sudo``, ``visudo``, ``launchctl``,
``ifconfig``, ``mkcert``, ``systemctl``, ``orbctl``) goes to :class:`FakeRunner`, which
records it and answers from a table: unless a test says otherwise every command succeeds,
so the machine looks like a Linux one running systemd-resolved. The per-domain resolver
configuration is plain files under the root (``/etc/resolver/<domain>``, the
systemd-resolved drop-in);
:func:`point_at_resolver` puts them there the way a finished ``setup`` leaves them. The
one command the runner really performs is a file written as the invoking user (``sudo -u
USER -H -- /bin/sh -c ...``, how setup writes that user's config): it runs, without the
``sudo``, against the temp directory. Nothing here touches the real machine or needs root.
"""

import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from pathlib import Path

from sandbox.platforms import Host

LO0_HEADER = (
    "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
    "\tinet 127.0.0.1 netmask 0xff000000\n"
)


@dataclass
class FakeRunner:
    # The first entry whose argv prefix matches answers; anything else succeeds silently.
    answers: list[tuple[list[str], int, str]] = field(
        default_factory=list[tuple[list[str], int, str]]
    )
    calls: list[list[str]] = field(default_factory=list[list[str]])

    def answer(self, prefix: Sequence[str], status: int = 0, output: str = "") -> None:
        self.answers.insert(0, (list(prefix), status, output))

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        for prefix, status, output in self.answers:
            if list(argv[: len(prefix)]) == prefix:
                return status, output
        if list(argv[:2]) == ["sudo", "-u"] and list(argv[3:6]) == ["-H", "--", "/bin/sh"]:
            done = subprocess.run(list(argv[5:]), capture_output=True, text=True, check=False)
            return done.returncode, (done.stdout + done.stderr).strip()
        return 0, ""

    def ran(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if call[: len(prefix)] == list(prefix)]


def lo0_with_aliases(*blocks: str) -> str:
    """``ifconfig lo0`` output with every host address of ``blocks`` aliased."""
    lines = [LO0_HEADER]
    for block in blocks:
        for ip in IPv4Network(block).hosts():
            lines.append(f"\tinet {ip} netmask 0xff000000\n")
    return "".join(lines)


def point_at_resolver(root: Path, domain: str = "test", *, platform: str = "linux") -> None:
    """Leave under ``root`` what ``sudo sandbox setup`` installs to send ``domain`` to the
    resolver: the systemd-resolved drop-in on Linux, ``/etc/resolver/<domain>`` on macOS."""
    if platform == "darwin":
        path = root / "etc" / "resolver" / domain
        text = "nameserver 127.0.0.1\nport 15353\n"
    else:
        path = root / "etc" / "systemd" / "resolved.conf.d" / "sandbox.conf"
        text = f"[Resolve]\nDNS=127.0.0.1:15353\nDomains=~{domain}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_host(
    root: Path,
    *,
    platform: str = "linux",
    runner: FakeRunner | None = None,
    euid: int = 0,
    sudo_user: str | None = "dev",
    path: str = "",
    homes: dict[str, Path] | None = None,
) -> Host:
    env = {"PATH": path}
    if sudo_user is not None:
        env["SUDO_USER"] = sudo_user
    known = homes if homes is not None else {"dev": root / "home" / "dev"}
    return Host(
        platform=platform,
        root=root,
        run=runner if runner is not None else FakeRunner(),
        euid=euid,
        env=env,
        python=Path(sys.executable),
        homes=known.get,
    )
