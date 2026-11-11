"""``sandbox SET down``: stop the set's services; keep worktrees, plan and set directory."""

import asyncio

import typer

from sandbox.config import load_config
from sandbox.context import current_set, failure, get_deps, warn
from sandbox.errors import SandboxError
from sandbox.provisioner import Provisioner


def down(ctx: typer.Context) -> None:
    """Stop the set's containers and remove its network and volumes (worktrees are kept)."""
    set_name = current_set(ctx, "down")
    try:
        deps = get_deps(ctx)
        provisioner = Provisioner(load_config(), deps.git, deps.compose, warn)
        result = asyncio.run(provisioner.down(set_name))
    except SandboxError as exc:
        failure(exc)
    typer.echo(
        f"set {result.set_name}: {result.project} stopped; worktrees kept in {result.set_dir}"
    )
