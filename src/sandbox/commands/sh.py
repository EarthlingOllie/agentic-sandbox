"""``sandbox SET sh [SERVICE] [--repo NAME]``: open a shell inside the set."""

import asyncio
from typing import Annotated

import typer

from sandbox.agent import TargetError, agent_target, load_plan, run_in, service_target
from sandbox.config import load_config
from sandbox.context import current_set, failure, get_deps, usage_error
from sandbox.errors import SandboxError


def sh(
    ctx: typer.Context,
    service: Annotated[
        str | None,
        typer.Argument(
            help="Open the shell in this service's container instead of the agent.",
            show_default=False,
        ),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option("--repo", metavar="NAME", help="Start in the agent's clone of this repo."),
    ] = None,
) -> None:
    """Open a shell in the set's agent (in /workspace/SET) or in one of its services."""
    set_name = current_set(ctx, "sh")
    if service is not None and repo is not None:
        usage_error("--repo applies to the agent's shell; it cannot be combined with SERVICE")
    try:
        plan = load_plan(load_config().sets_root, set_name)
        if service is None:
            target = agent_target(set_name, plan, repo)
        else:
            target = service_target(set_name, plan, service)
    except TargetError as exc:
        usage_error(str(exc))
    except SandboxError as exc:
        failure(exc)
    try:
        status = asyncio.run(run_in(get_deps(ctx).compose, target, ["sh"]))
    except SandboxError as exc:
        failure(exc)
    raise typer.Exit(code=status)
