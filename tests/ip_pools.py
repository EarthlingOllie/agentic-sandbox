"""Host-wide claims on loopback /16s, so concurrent test runs never share an IP:port.

Every service a set runs publishes ``<leased IP>:<native port>`` on the host, and each
test run leases from a fresh lease file, so two runs leasing from the same pool hand out
the same addresses and the second ``up`` fails with "port is already allocated". A run
therefore claims a /16 of its own: an exclusive ``flock`` on ``127.<octet>.lock`` in a
directory every worktree shares. The claim lasts as long as the open handle, so it ends
when the pytest process does, crash included; no stale claim outlives its run.
"""

import fcntl
import tempfile
from ipaddress import IPv4Network
from pathlib import Path
from typing import IO, Final

POOL_CLAIMS_DIR: Final = Path(tempfile.gettempdir()) / "sandbox-test-ip-pools"
# Second octets a run may claim. Never 0/1 (localhost) or 42 (the default pool real sets
# use). Octets 64-247 come last: older checkouts hashed their run ID into that range
# without claiming, so a run still on that code can't meet one that claimed elsewhere.
CLAIMABLE_OCTETS: Final = (*range(2, 42), *range(43, 64), *range(248, 255), *range(64, 248))


def claim_run_pool(
    claims_dir: Path = POOL_CLAIMS_DIR, octets: tuple[int, ...] = CLAIMABLE_OCTETS
) -> tuple[IPv4Network, IO[bytes]]:
    """Claim a loopback /16 no other live run holds; it stays claimed while the handle is open."""
    claims_dir.mkdir(parents=True, exist_ok=True)
    for octet in octets:
        handle = (claims_dir / f"127.{octet}.lock").open("ab")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            continue
        return IPv4Network(f"127.{octet}.0.0/16"), handle
    raise RuntimeError(f"every test IP pool in {claims_dir} is claimed by a live run")
