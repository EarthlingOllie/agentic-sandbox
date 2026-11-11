"""The hosts file: one ``# sandbox:<set>`` block per set, holding exactly its hostnames.

``up`` writes the set's block once its services are up (a provision step, so rollback
removes it again), and ``down`` removes it. Lines outside a set's own block are never
touched. Every rewrite reads, changes and replaces the file under an exclusive ``flock``,
so concurrent ``sandbox`` processes never lose each other's blocks.

Two writers share that logic:

- :class:`HelperHostsWriter` (production, ``hosts_file`` unset): runs the root-owned
  ``sandbox-hosts`` helper (``sandbox.hosts_helper``) through ``sudo -n`` on ``/etc/hosts``,
  so it never prompts.
- :class:`DirectHostsWriter` (``hosts_file`` set in the config): writes that file itself.
  Tests point it at a temp file.

Both refuse an IP outside the set's /24 and a name outside the set's ``.test`` domain
(:func:`check_entries`), so the helper cannot be abused to hijack a real domain.
"""

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Final, Protocol

from sandbox.bindings import bindings
from sandbox.compose.network import TLD
from sandbox.compose.web import extra_hostnames
from sandbox.config import LOOPBACK, Config
from sandbox.errors import SandboxError
from sandbox.leases import BLOCK_PREFIX
from sandbox.locking import file_lock
from sandbox.names import validate_set_name
from sandbox.pipeline import UpContext

SYSTEM_HOSTS_FILE: Final = Path("/etc/hosts")
HELPER: Final = "sandbox-hosts"
# One lowercase DNS label: letters, digits and inner hyphens.
_LABEL: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# hostname -> IP
Entries = Mapping[str, IPv4Address]


def block_start(set_name: str) -> str:
    return f"# sandbox:{set_name}"


def block_end(set_name: str) -> str:
    return f"# end sandbox:{set_name}"


def check_entries(set_name: str, block: IPv4Network, entries: Entries) -> None:
    """Refuse anything but the set's own loopback /24 and ``<label>.<set>.test`` names."""
    validate_set_name(set_name)
    problems: list[str] = []
    if block.prefixlen != BLOCK_PREFIX or not block.subnet_of(LOOPBACK):
        problems.append(f"block {block} is not a loopback /{BLOCK_PREFIX}")
    domain = f".{set_name}.{TLD}"
    for name, ip in sorted(entries.items()):
        if not name.endswith(f".{TLD}"):
            problems.append(f"{name!r} does not end in .{TLD}")
        elif not name.endswith(domain) or not all(
            _LABEL.fullmatch(label) for label in name.removesuffix(domain).split(".")
        ):
            problems.append(f"{name!r} is not a hostname under {domain[1:]}")
        if ip not in block or ip in (block.network_address, block.broadcast_address):
            problems.append(f"{ip} ({name}) is outside the set's block {block}")
    if problems:
        raise SandboxError(f"refusing hosts entries for set {set_name!r}: " + "; ".join(problems))


def render_block(set_name: str, entries: Entries) -> str:
    lines = [block_start(set_name)]
    lines += [f"{ip}\t{name}" for name, ip in sorted(entries.items())]
    lines.append(block_end(set_name))
    return "\n".join(lines) + "\n"


def replace_block(text: str, set_name: str, entries: Entries) -> str:
    """``text`` with the set's block replaced (in place), appended, or removed (no entries)."""
    lines = text.splitlines(keepends=True)
    stripped = [line.rstrip("\r\n") for line in lines]
    start, end = block_start(set_name), block_end(set_name)
    new = render_block(set_name, entries) if entries else ""
    if start not in stripped:
        if not new:
            return text
        return text + ("\n" if text and not text.endswith("\n") else "") + new
    first = stripped.index(start)
    if end not in stripped[first:]:
        raise SandboxError(
            f"the hosts file has {start!r} without a matching {end!r}; "
            "fix the block by hand, then rerun"
        )
    last = stripped.index(end, first)
    return "".join(lines[:first]) + new + "".join(lines[last + 1 :])


async def update_hosts_file(path: Path, lock_path: Path, set_name: str, entries: Entries) -> None:
    """Rewrite only the set's block of ``path`` under an exclusive ``flock`` on ``lock_path``."""
    async with file_lock(lock_path):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        updated = replace_block(text, set_name, entries)
        if updated != text:
            _replace_file(path, updated)


def _replace_file(path: Path, text: str) -> None:
    """Atomically replace ``path`` (keeping its mode), so resolvers never see half a file.

    Falls back to writing in place where the file cannot be renamed over, e.g. a
    bind-mounted ``/etc/hosts`` inside a container.
    """
    temporary = path.with_name(f".{path.name}.sandbox-tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        if path.exists():
            shutil.copymode(path, temporary)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")


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
