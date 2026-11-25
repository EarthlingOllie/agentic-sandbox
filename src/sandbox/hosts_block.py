"""The ``# sandbox:<set>`` block of a hosts file: validate, render, replace (stdlib only).

This module is part of the root-owned ``sandbox-hosts`` helper bundle that
``sudo sandbox setup`` installs (``sandbox.setup.HELPER_MODULES``). The helper runs under the
system's Python with ``-I -S``, so this module and everything it imports must use the
standard library only and stay importable on Python 3.9 (macOS's ``/usr/bin/python3``).
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Final

from sandbox.errors import SandboxError
from sandbox.locking import file_lock
from sandbox.names import DEFAULT_DOMAIN, DNS_LABEL, validate_domain, validate_set_name

SYSTEM_HOSTS_FILE: Final = Path("/etc/hosts")
# Where `sudo sandbox setup` installs the helper; the sudoers rule names this exact path.
HELPER_PATH: Final = Path("/usr/local/bin/sandbox-hosts")
# Mirrors of sandbox.config.LOOPBACK and sandbox.leases.BLOCK_PREFIX, kept here so the
# helper bundle needs nothing else.
LOOPBACK: Final = IPv4Network("127.0.0.0/8")
BLOCK_PREFIX: Final = 24

# hostname -> IP
Entries = Mapping[str, IPv4Address]


def block_start(set_name: str) -> str:
    return f"# sandbox:{set_name}"


def block_end(set_name: str) -> str:
    return f"# end sandbox:{set_name}"


def check_entries(
    set_name: str, block: IPv4Network, entries: Entries, domain: str = DEFAULT_DOMAIN
) -> None:
    """Refuse anything but the set's own loopback /24 and ``<label>.<set>.<domain>`` names
    (``domain`` is the config's; it must itself be a valid one)."""
    validate_set_name(set_name)
    try:
        validate_domain(domain)
    except ValueError as exc:
        raise SandboxError(f"refusing hosts entries for set {set_name!r}: domain {exc}") from exc
    problems: list[str] = []
    if block.prefixlen != BLOCK_PREFIX or not block.subnet_of(LOOPBACK):
        problems.append(f"block {block} is not a loopback /{BLOCK_PREFIX}")
    suffix = f".{set_name}.{domain}"
    for name, ip in sorted(entries.items()):
        if not name.endswith(f".{domain}"):
            problems.append(f"{name!r} does not end in .{domain}")
        elif not name.endswith(suffix) or not all(
            DNS_LABEL.fullmatch(label) for label in name[: -len(suffix)].split(".")
        ):
            problems.append(f"{name!r} is not a hostname under {suffix[1:]}")
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


def outside_blocks(text: str) -> list[str]:
    """The lines of a hosts file that no ``# sandbox:<set>`` block holds."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if not inside and stripped.startswith("# sandbox:"):
            inside = True
        elif inside and stripped.startswith("# end sandbox:"):
            inside = False
        elif not inside:
            kept.append(line)
    return kept


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
