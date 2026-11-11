"""``sandbox [SET] prune``: clear a stopped set's infrastructure, or every orphan."""

import asyncio

import typer

from sandbox.config import load_config
from sandbox.context import SET_META_KEY, failure, get_deps
from sandbox.errors import SandboxError
from sandbox.prune import Pruner


def prune(ctx: typer.Context) -> None:
    """Remove a stopped set's containers, network, volumes and hosts block, and release
    its IP lease; without SET, do that for every orphaned set. Worktrees are never touched.
    """
    set_name = ctx.meta.get(SET_META_KEY)
    try:
        deps = get_deps(ctx)
        pruner = Pruner(load_config(), deps.git, deps.compose, deps.docker)
        if isinstance(set_name, str):
            pruned = asyncio.run(pruner.prune_set(set_name))
            typer.echo(
                f"set {pruned.set_name}: pruned {pruned.project} (containers, network, "
                "volumes, hosts block, IP lease); plan and worktrees kept"
            )
            return
        report = asyncio.run(pruner.prune_orphans())
    except SandboxError as exc:
        failure(exc)
    if not report.pruned and not report.failed:
        typer.echo("no orphaned sets to prune")
        return
    for done in report.pruned:
        typer.echo(f"set {done.set_name}: pruned {done.project}; worktrees kept")
    for failed in report.failed:
        typer.echo(f"error: set {failed.set_name}: prune failed: {failed.error}", err=True)
    if report.failed:
        raise typer.Exit(code=1)
