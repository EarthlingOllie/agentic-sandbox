"""``sandbox-hosts``: the small privileged helper that edits ``/etc/hosts`` for sandbox.

``sudo sandbox setup`` (ticket 18) installs it root-owned with a NOPASSWD sudoers rule
scoped to this binary, and ``sandbox`` runs it as ``sudo -n sandbox-hosts ...``::

    sandbox-hosts write SET BLOCK HOSTNAME=IP...   # make SET's block hold exactly these
    sandbox-hosts remove SET                       # drop SET's block

It always edits ``/etc/hosts`` (the path is not an argument, so a caller cannot aim root at
another file), rewrites only the named set's ``# sandbox:<set>`` block under an exclusive
``flock``, and refuses any IP outside the set's loopback /24 and any name that is not a
``<label>.<set>.test`` hostname (:func:`sandbox.hosts.check_entries`).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from ipaddress import AddressValueError, IPv4Address, IPv4Network, NetmaskValueError
from pathlib import Path
from typing import Final, NoReturn

from sandbox.errors import SandboxError
from sandbox.hosts import SYSTEM_HOSTS_FILE, check_entries, update_hosts_file
from sandbox.names import validate_set_name

# Root's lock, beside other system run-time state (/var/run exists on Linux and macOS).
SYSTEM_LOCK_FILE: Final = Path("/var/run/sandbox-hosts.lock")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise SandboxError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="sandbox-hosts", description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    write = verbs.add_parser("write", help="make the set's block hold exactly these entries")
    write.add_argument("set_name", metavar="SET")
    write.add_argument("block", metavar="BLOCK", help="the set's loopback /24")
    write.add_argument("entries", metavar="HOSTNAME=IP", nargs="+")
    remove = verbs.add_parser("remove", help="remove the set's block")
    remove.add_argument("set_name", metavar="SET")
    return parser


def _entries(pairs: Sequence[str]) -> dict[str, IPv4Address]:
    entries: dict[str, IPv4Address] = {}
    for pair in pairs:
        name, sep, ip = pair.partition("=")
        if not sep:
            raise SandboxError(f"{pair!r} is not HOSTNAME=IP")
        try:
            entries[name] = IPv4Address(ip)
        except AddressValueError as exc:
            raise SandboxError(f"{pair!r}: {exc}") from exc
    return entries


def run(
    argv: Sequence[str],
    *,
    hosts_path: Path = SYSTEM_HOSTS_FILE,
    lock_path: Path = SYSTEM_LOCK_FILE,
) -> int:
    """Run the helper's command line against ``hosts_path``; returns the exit status."""
    try:
        args = _parser().parse_args(list(argv))
        set_name = validate_set_name(str(args.set_name))
        if args.verb == "write":
            try:
                block = IPv4Network(str(args.block))
            except (AddressValueError, NetmaskValueError, ValueError) as exc:
                raise SandboxError(f"block {args.block!r}: {exc}") from exc
            entries = _entries([str(entry) for entry in args.entries])
            check_entries(set_name, block, entries)
        else:
            entries = {}
        asyncio.run(update_hosts_file(hosts_path, lock_path, set_name, entries))
    except SandboxError as exc:
        print(f"sandbox-hosts: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    """Console-script entry point (``sandbox-hosts``): always edits ``/etc/hosts``."""
    sys.exit(run(sys.argv[1:]))
