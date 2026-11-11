"""The hosts file: one ``# sandbox:<set>`` block per set, holding exactly its hostnames.

``up`` writes the set's block once its services are up (a provision step, so rollback
removes it again), and ``down`` removes it. Lines outside a set's own block are never
touched. Every rewrite reads, changes and replaces the file under an exclusive ``flock``,
so concurrent ``sandbox`` processes never lose each other's blocks.

Two writers share that logic:

- :class:`HelperHostsWriter` (production, ``hosts_file`` unset): runs the root-owned
  ``sandbox-hosts`` helper (``sandbox.hosts_helper``, installed by ``sudo sandbox setup`` at
  ``/usr/local/bin/sandbox-hosts``) through ``sudo -n`` on ``/etc/hosts``, so it never prompts.
- :class:`DirectHostsWriter` (``hosts_file`` set in the config): writes that file itself.
  Tests point it at a temp file.

Both refuse an IP outside the set's /24 and a name outside the set's ``.test`` domain
(:func:`check_entries`), so the helper cannot be abused to hijack a real domain. The block
logic itself lives in ``sandbox.hosts_block`` (standard library only), which the installed
helper bundles.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from ipaddress import IPv4Network
from pathlib import Path
from typing import Final, Protocol

from sandbox.bindings import bindings
from sandbox.compose.web import extra_hostnames
from sandbox.config import Config
from sandbox.errors import SandboxError
from sandbox.hosts_block import (
    HELPER_PATH,
    SYSTEM_HOSTS_FILE,
    Entries,
    block_end,
    block_start,
    check_entries,
    render_block,
    replace_block,
    update_hosts_file,
)
from sandbox.names import validate_set_name
from sandbox.pipeline import UpContext

__all__ = [
    "HELPER",
    "SYSTEM_HOSTS_FILE",
    "DirectHostsWriter",
    "Entries",
    "HelperHostsWriter",
    "HostsWriter",
    "block_end",
    "block_start",
    "check_entries",
    "hosts_writer",
    "remove_hosts_block",
    "render_block",
    "replace_block",
    "update_hosts_file",
    "write_hosts_block",
]

# The root-owned copy `sudo sandbox setup` installs, by absolute path: the sudoers rule
# names exactly this file, and sudo must not resolve the name through the caller's PATH.
HELPER: Final = str(HELPER_PATH)


class HostsWriter(Protocol):
    async def write(self, set_name: str, block: IPv4Network, entries: Entries) -> None:
        """Make the set's block hold exactly ``entries`` (no entries removes it)."""
        ...

    async def remove(self, set_name: str) -> None:
        """Remove the set's block, if any."""
        ...


class DirectHostsWriter:
    """Writes the hosts file itself (``hosts_file`` set in the config)."""

    def __init__(self, path: Path, lock_path: Path) -> None:
        self._path = path
        self._lock_path = lock_path

    async def write(self, set_name: str, block: IPv4Network, entries: Entries) -> None:
        check_entries(set_name, block, entries)
        await update_hosts_file(self._path, self._lock_path, set_name, entries)

    async def remove(self, set_name: str) -> None:
        validate_set_name(set_name)
        await update_hosts_file(self._path, self._lock_path, set_name, {})


# argv -> (exit status, combined output)
Runner = Callable[[Sequence[str]], Awaitable[tuple[int, str]]]


async def run_command(argv: Sequence[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode(errors="replace").strip()


class HelperHostsWriter:
    """Writes ``/etc/hosts`` through ``sudo -n sandbox-hosts`` (never prompts for a password)."""

    def __init__(
        self,
        runner: Runner = run_command,
        helper: str = HELPER,
        path: Path = SYSTEM_HOSTS_FILE,
    ) -> None:
        self._runner = runner
        self._prefix = ["sudo", "-n", helper]
        self._path = path  # read (unprivileged) only to skip sudo when nothing would change

    async def write(self, set_name: str, block: IPv4Network, entries: Entries) -> None:
        check_entries(set_name, block, entries)  # fail with the same message without sudo
        if self._unchanged(set_name, entries):
            return
        pairs = [f"{name}={ip}" for name, ip in sorted(entries.items())]
        await self._run(["write", set_name, str(block), *pairs])

    async def remove(self, set_name: str) -> None:
        validate_set_name(set_name)
        if self._unchanged(set_name, {}):
            return
        await self._run(["remove", set_name])

    def _unchanged(self, set_name: str, entries: Entries) -> bool:
        try:
            text = self._path.read_text(encoding="utf-8")
            return replace_block(text, set_name, entries) == text
        except (OSError, SandboxError):
            return False  # let the helper decide

    async def _run(self, args: list[str]) -> None:
        argv = [*self._prefix, *args]
        try:
            status, output = await self._runner(argv)
        except OSError as exc:
            raise SandboxError(f"could not run {' '.join(self._prefix)}: {exc}") from exc
        if status != 0:
            raise SandboxError(
                f"could not update {SYSTEM_HOSTS_FILE}: `{' '.join(argv)}` exited {status}"
                + (f": {output}" if output else "")
                + "\nthe hosts helper and its sudoers rule are installed by `sudo sandbox setup`"
            )


def hosts_writer(config: Config) -> HostsWriter:
    if config.hosts_file is not None:
        return DirectHostsWriter(config.hosts_file, config.lock_dir / "hosts.lock")
    return HelperHostsWriter()


# --- up steps --------------------------------------------------------------------------


def _writer(ctx: UpContext) -> HostsWriter:
    return ctx.hosts if ctx.hosts is not None else hosts_writer(ctx.config)


async def write_hosts_block(ctx: UpContext) -> None:
    """Post-up step: the set's block holds exactly its current hostnames and IPs."""
    found = bindings(ctx.set_name, ctx.graph, ctx.lease, extra_hostnames(ctx.web_apps))
    if ctx.lease is None or not found:
        await _writer(ctx).remove(ctx.set_name)  # a no-op unless services left the plan
        return
    await _writer(ctx).write(
        ctx.set_name, ctx.lease.block, {binding.hostname: binding.ip for binding in found}
    )


async def remove_hosts_block(ctx: UpContext) -> None:
    await _writer(ctx).remove(ctx.set_name)
