"""Prerequisite checks: ``up`` fails before creating anything if setup has not been run.

The ``prerequisites`` validation step runs before the set leases IPs (the first thing ``up``
writes) and checks what ``sudo sandbox setup`` (``sandbox.setup``) installs, naming each
missing piece:

- the hosts helper and its NOPASSWD sudoers rule, when the hosts block goes into
  ``/etc/hosts`` (``hosts_file`` unset in the config). ``sudo -n -l HELPER`` asks sudo
  itself whether the rule lets this user run the helper without a password;
- on macOS, the ``lo0`` alias pool: the launchd job, and how many of the pool's /24 blocks
  are aliased right now. Only those blocks are leased, so when they are all taken ``up``
  fails asking for a larger ``--sets``. Linux needs no aliases (127.0.0.0/8 is local).

The mkcert CA (``rootCA.pem``) is checked by the ``web-apps`` step, for sets that serve
HTTPS (``sandbox.web``). Sets without services need none of this.
"""

import re
from ipaddress import IPv4Address, IPv4Network

from sandbox.bindings import lease_store
from sandbox.errors import SandboxError
from sandbox.hosts_block import HELPER_PATH
from sandbox.leases import BLOCK_PREFIX
from sandbox.pipeline import UpContext
from sandbox.platforms import Host
from sandbox.setup import LAUNCHD_PLIST, SETUP_COMMAND

_INET = re.compile(r"\binet (\d+\.\d+\.\d+\.\d+)")


async def check_prerequisites(ctx: UpContext) -> None:
    """Validation step: fail naming every missing prerequisite of a set with services."""
    if not ctx.graph.services:
        return
    problems: list[str] = []
    if ctx.config.hosts_file is None:
        problem = await helper_problem(ctx.host)
        if problem:
            problems.append(problem)
    if ctx.host.is_macos:
        try:
            ctx.alias_blocks = await _alias_pool(ctx)
        except SandboxError as exc:
            problems.append(str(exc))
    if problems:
        raise SandboxError("setup is incomplete:\n  " + "\n  ".join(problems))


async def helper_problem(host: Host) -> str | None:
    if not host.path(HELPER_PATH).is_file():
        return f"the hosts helper {HELPER_PATH} is not installed; run `{SETUP_COMMAND}`"
    status, _ = await host.run(["sudo", "-n", "-l", str(HELPER_PATH)])
    if status != 0:
        return (
            f"there is no passwordless sudo rule for {HELPER_PATH} for this user; "
            f"run `{SETUP_COMMAND}` as this user"
        )
    return None


async def aliased_ips(host: Host) -> set[IPv4Address]:
    """The IPv4 addresses on ``lo0`` right now (macOS)."""
    status, output = await host.run(["ifconfig", "lo0"])
    if status != 0:
        raise SandboxError(f"could not read the lo0 aliases: `ifconfig lo0` failed: {output}")
    return {IPv4Address(ip) for ip in _INET.findall(output)}


def aliased_blocks(pool: IPv4Network, aliased: set[IPv4Address]) -> int:
    """How many of ``pool``'s /24s, from its start, have every host address aliased."""
    count = 0
    for block in pool.subnets(new_prefix=BLOCK_PREFIX):
        if not all(ip in aliased for ip in block.hosts()):
            break
        count += 1
    return count


async def _alias_pool(ctx: UpContext) -> int:
    pool = ctx.config.ip_pool
    if not ctx.host.path(LAUNCHD_PLIST).is_file():
        raise SandboxError(
            f"the loopback alias pool is not installed (no launchd job {LAUNCHD_PLIST}); "
            f"run `{SETUP_COMMAND}`"
        )
    count = aliased_blocks(pool, await aliased_ips(ctx.host))
    if count == 0:
        raise SandboxError(
            f"the loopback alias pool's launchd job is installed, but there are no lo0 "
            f"aliases in {pool} (the job adds them at boot); run `{SETUP_COMMAND}` to add "
            "them now"
        )
    lease = lease_store(ctx.config).read(ctx.set_name)
    blocks = list(pool.subnets(new_prefix=BLOCK_PREFIX))
    if lease is not None and lease.block in blocks and blocks.index(lease.block) >= count:
        raise SandboxError(
            f"set {ctx.set_name!r}'s block {lease.block} has no lo0 aliases (the pool covers "
            f"{blocks[0]} to {blocks[count - 1]}); rerun "
            f"`{SETUP_COMMAND} --sets {blocks.index(lease.block) + 1}` or larger"
        )
    return count
