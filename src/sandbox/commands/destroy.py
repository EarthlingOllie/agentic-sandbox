"""``sandbox SET destroy``: sync and prune a set, then remove its clones and set directory."""

import asyncio
from typing import Annotated

import typer

from sandbox.config import load_config
from sandbox.context import current_set, failure, get_deps
from sandbox.destroy import Destroyer
from sandbox.errors import SandboxError


def destroy(
    ctx: typer.Context,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Discard uncommitted and untracked changes in the agent's clones and agent "
            "commits that couldn't be synced; with --branches, also delete branches holding "
            "unpushed commits.",
        ),
    ] = False,
    branches: Annotated[
        bool,
        typer.Option(
            "--branches",
            help="Also delete the agent/SET branches (refused on unpushed commits unless --force).",
        ),
    ] = False,
) -> None:
    """Remove a stopped set for good: sync agent/SET into each repo, prune the set, then
    remove the agent's clones and the set directory (plan, certificate). Refuses on a
    running set, and on agent commits that couldn't be synced or uncommitted or untracked
    work unless --force. The agent/SET branches are kept unless --branches.
    """
    set_name = current_set(ctx, "destroy")
    try:
        deps = get_deps(ctx)
        destroyer = Destroyer(load_config(), deps.git, deps.compose, deps.docker)
        result = asyncio.run(destroyer.destroy(set_name, force=force, branches=branches))
    except SandboxError as exc:
        failure(exc)
    typer.echo(f"set {result.set_name}: destroyed {result.set_dir}")
    for path in result.clones:
        typer.echo(f"  removed clone {path}")
    for path in result.root_cleaned:
        typer.echo(f"  deleted root-owned files in {path} from a throwaway container")
    for source in result.deleted_branches:
        typer.echo(f"  deleted branch {result.branch} in {source}")
    for source in result.kept_branches:
        typer.echo(f"  kept branch {result.branch} in {source} (delete it with --branches)")
