"""Loopback IP leases: each set leases one /24 from the pool; each hostname one IP in it.

Every hostname in a set gets its own loopback IP, so services keep their native ports and
``mysql.jira-123.test:3306`` sits beside ``mysql.jira-456.test:3306``. The lease file
(``<state_dir>/leases.json``) is read, changed and rewritten only under an exclusive
``flock``, so concurrent ``up`` processes never hand out the same block or IP.

Leases are sticky: a set keeps its block, and a hostname its IP, across ``down`` and ``up``
(and even when the hostname leaves the set). Only ``prune`` releases a set's lease.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Final, cast

from sandbox.errors import SandboxError
from sandbox.locking import file_lock

BLOCK_PREFIX: Final = 24
HOSTS_PER_BLOCK: Final = 254  # .1 to .254


@dataclass(frozen=True)
class SetLease:
    set_name: str
    block: IPv4Network
    # hostname -> IP, sorted by hostname
    ips: dict[str, IPv4Address]


class LeaseStore:
    def __init__(self, path: Path, lock_path: Path, pool: IPv4Network) -> None:
        self._path = path
        self._lock_path = lock_path
        self._pool = pool

    def read(self, set_name: str) -> SetLease | None:
        """The set's current lease, without leasing anything (a lock-free snapshot)."""
        return self._load().get(set_name)

    def read_all(self) -> dict[str, SetLease]:
        """Every set's current lease, by set name (a lock-free snapshot)."""
        return self._load()

    async def lease(self, set_name: str, hostnames: Iterable[str]) -> SetLease:
        """Lease the set's block (or keep it) and an IP for every hostname (or keep it)."""
        async with file_lock(self._lock_path):
            leases = self._load()
            current = leases.get(set_name)
            block = current.block if current else self._free_block(leases)
            ips = dict(current.ips) if current else {}
            used = set(ips.values())
            free = (ip for ip in block.hosts() if ip not in used)
            for hostname in sorted(set(hostnames) - ips.keys()):
                ip = next(free, None)
                if ip is None:
                    raise SandboxError(
                        f"set {set_name!r} needs more than {HOSTS_PER_BLOCK} hostnames; "
                        f"its block {block} is full"
                    )
                ips[hostname] = ip
            lease = SetLease(set_name, block, dict(sorted(ips.items())))
            if lease != current:
                leases[set_name] = lease
                self._save(leases)
            return lease

    async def release(self, set_name: str) -> None:
        """Give the set's block back to the pool (``prune`` only)."""
        async with file_lock(self._lock_path):
            leases = self._load()
            if leases.pop(set_name, None) is not None:
                self._save(leases)

    def _free_block(self, leases: dict[str, SetLease]) -> IPv4Network:
        taken = {lease.block for lease in leases.values()}
        for block in self._pool.subnets(new_prefix=BLOCK_PREFIX):
            if block not in taken:
                return block
        raise SandboxError(
            f"no free /{BLOCK_PREFIX} left in {self._pool}: every block is leased to a set; "
            "free some with `sandbox prune`, or widen `ip_pool` in the config"
        )

    def _load(self) -> dict[str, SetLease]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(text)
            sets = cast(dict[str, Any], data["sets"])
            return {
                name: SetLease(
                    name,
                    IPv4Network(entry["block"]),
                    {host: IPv4Address(ip) for host, ip in entry["ips"].items()},
                )
                for name, entry in sets.items()
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SandboxError(
                f"lease file {self._path} is corrupt ({exc}); fix or remove it "
                "(removing it forgets every set's IPs)"
            ) from exc

    def _save(self, leases: dict[str, SetLease]) -> None:
        data = {
            "sets": {
                name: {
                    "block": str(lease.block),
                    "ips": {host: str(ip) for host, ip in lease.ips.items()},
                }
                for name, lease in sorted(leases.items())
            }
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
