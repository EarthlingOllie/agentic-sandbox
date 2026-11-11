"""``sudo sandbox setup [--uninstall] [--sets N]``: the one-time privileged install."""

import asyncio
from typing import Annotated

import typer

from sandbox.context import SET_META_KEY, failure, get_deps, usage_error
from sandbox.errors import SandboxError
from sandbox.setup import DEFAULT_SETS, install, uninstall


def setup(
    ctx: typer.Context,
    remove: Annotated[
        bool,
        typer.Option(
            "--uninstall",
            help="Remove the hosts helper, its sudoers rule and the launchd job "
            "(the mkcert CA is kept).",
        ),
    ] = False,
    sets: Annotated[
        int,
        typer.Option(
            "--sets",
            metavar="N",
            help="macOS: how many sets can run at once (lo0 alias /24 blocks).",
        ),
    ] = DEFAULT_SETS,
) -> None:
    """Install what sandbox needs root for (run once, with sudo): the hosts helper and its
    sudoers rule, the macOS loopback alias pool, and the mkcert CA (as you, not root).
    """
    if isinstance(ctx.meta.get(SET_META_KEY), str):
        usage_error("setup takes no set: sudo sandbox setup [--uninstall] [--sets N]")
    host = get_deps(ctx).host
    try:
        result = asyncio.run(uninstall(host) if remove else install(host, sets))
    except SandboxError as exc:
        failure(exc)
    typer.echo(f"sandbox setup {'removed' if remove else 'installed'}:")
    for line in result.lines:
        typer.echo(f"  {line}")
