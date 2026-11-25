"""The resolver around a set: ``up`` starts and attaches it, everything that removes a
set's network detaches it first.

One resolver (``sandbox.compose.resolver``) serves every set. The ``resolver`` provision
step makes sure it runs, serialised across processes with a file lock so concurrent ``up``s
start one, and attaches it to the set's network; from then on a lookup of
``<service>.<set>.<domain>`` sent to it returns that container's address. Docker refuses
to remove a network with a container on it, so ``down``, a rolled-back ``up``, ``up
--recreate``, ``prune`` and ``destroy`` detach it before they remove the set's project.
Attaching and detaching are idempotent, and the resolver itself is never stopped here.

A set without services has nothing to look up, so its ``up`` does not start the resolver.
"""

from typing import Final

from sandbox.compose.resolver import set_network
from sandbox.docker import Docker
from sandbox.locking import file_lock
from sandbox.pipeline import UpContext

LOCK_NAME: Final = "resolver.lock"


async def attach_resolver(ctx: UpContext) -> None:
    """Ensure the resolver runs for the configured domain and is on the set's network."""
    if not ctx.services:
        return
    async with file_lock(ctx.config.lock_dir / LOCK_NAME):
        await ctx.docker.ensure_resolver(ctx.config.domain)
    await ctx.docker.attach_resolver(set_network(ctx.set_name))


async def detach_resolver(ctx: UpContext) -> None:
    await detach(ctx.docker, ctx.set_name)


async def detach(docker: Docker, set_name: str) -> None:
    """Take the resolver off the set's network (a no-op when it isn't on it)."""
    await docker.detach_resolver(set_network(set_name))
