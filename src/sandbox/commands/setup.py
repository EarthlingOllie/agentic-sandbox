"""``sudo sandbox setup [--domain X] [--uninstall] [--sets N]``: the one-time privileged
install."""

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
            help="Remove the resolver configuration, the resolver, the hosts helper, its "
            "sudoers rule and the launchd job (the mkcert CA is kept).",
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
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            metavar="DOMAIN",
            help="The DNS suffix of set hostnames; recorded in your config. "
            "Default: the configured domain, or test.",
        ),
    ] = None,
) -> None:
    """Install what sandbox needs root for (run once, with sudo): point this machine's DNS
    lookups under the domain at the resolver, the mkcert CA (as you, not root), the hosts
    helper and its sudoers rule, and the macOS loopback alias pool.
    """
    if isinstance(ctx.meta.get(SET_META_KEY), str):
        usage_error("setup takes no set: sudo sandbox setup [--domain X] [--uninstall] [--sets N]")
    if remove and domain is not None:
        usage_error(
            "--uninstall takes no --domain: it removes the configuration of whichever domain "
            "was installed"
        )
    deps = get_deps(ctx)
    host = deps.host
    try:
        result = asyncio.run(
            uninstall(host, deps.docker) if remove else install(host, sets, domain)
        )
    except SandboxError as exc:
        failure(exc)
    typer.echo(f"sandbox setup {'removed' if remove else 'installed'}:")
    for line in result.lines:
        typer.echo(f"  {line}")
