"""The ``sandbox`` command line: ``sandbox [SET] <verb> [OPTIONS]``.

The optional leading SET is peeled off before Click dispatches the verb and stored in the
context's shared ``meta``, so every verb can read it with :func:`current_set`. Dependencies
(git, Compose; mkcert later) travel in ``ctx.obj`` so tests can invoke the app in-process
with fakes: ``CliRunner().invoke(app, [...], obj=Deps(git=FakeGit(), compose=FakeCompose()))``.
Verbs other than ``up`` live in ``sandbox.commands.<verb>`` and are registered below with one
``app.command`` line each; the helpers they share are in ``sandbox.context``.
"""

import asyncio
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from sandbox.bindings import format_bindings
from sandbox.commands.claude import claude
from sandbox.commands.down import down
from sandbox.commands.prune import prune
from sandbox.commands.save import save
from sandbox.commands.sh import sh
from sandbox.commands.status import status
from sandbox.commands.templates import templates
from sandbox.config import load_config
from sandbox.context import (
    SET_META_KEY,
    Deps,
    current_set,
    failure,
    get_deps,
    usage_error,
    warn,
)
from sandbox.errors import SandboxError
from sandbox.names import RESERVED_VERBS, validate_set_name
from sandbox.platforms import ensure_supported_platform
from sandbox.provisioner import Provisioner
from sandbox.state import UpAction
from sandbox.templates import TemplateStore
from sandbox.worktrees import WorktreeOutcome


class SetFirstGroup(TyperGroup):
    """A group that accepts an optional set name before the verb."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:  # noqa: ANN401 - Click's Context is private in Typer
        if args and not args[0].startswith("-"):
            first = args[0]
            if first in RESERVED_VERBS:
                if len(args) > 1 and args[1] in RESERVED_VERBS:
                    usage_error(f"{first!r} is a sandbox verb and cannot be used as a set name")
            else:
                try:
                    ctx.meta[SET_META_KEY] = validate_set_name(first)
                except SandboxError as exc:
                    usage_error(str(exc))
                args = args[1:]
                if not args:
                    usage_error(f"missing verb: sandbox {first} <verb>")
        return super().parse_args(ctx, args)


app = typer.Typer(
    cls=SetFirstGroup,
    name="sandbox",
    help="Provision isolated multi-repo sets (worktrees, services, agent) for coding agents.",
    subcommand_metavar="[SET] VERB [ARGS]...",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


@app.callback()
def _root(ctx: typer.Context) -> None:
    try:
        ensure_supported_platform()
    except SandboxError as exc:
        usage_error(str(exc))
    if ctx.obj is None:
        ctx.obj = Deps()


@app.command()
def up(
    ctx: typer.Context,
    use_repos: Annotated[
        bool,
        typer.Option(
            "--repos",
            help="Build a new set from the NAME directories in the current directory.",
        ),
    ] = False,
    names: Annotated[
        list[str] | None,
        typer.Argument(metavar="NAME...", show_default=False),
    ] = None,
    from_template: Annotated[
        str | None,
        typer.Option(
            "--from",
            metavar="TEMPLATE",
            help="Build a new set from a template (see `sandbox templates`).",
        ),
    ] = None,
    recreate: Annotated[
        bool,
        typer.Option("--recreate", help="Restart the set's services even if already running."),
    ] = False,
) -> None:
    """Create the set, or resume it: a worktree per repo on agent/SET, services healthy.

    A running set is left alone (use --recreate to restart it). If `up` fails or is
    interrupted, the set's services are rolled back; plan and worktrees are kept.
    """
    set_name = current_set(ctx, "up")
    if from_template is not None and (use_repos or names):
        usage_error("--from and --repos are mutually exclusive")
    if use_repos and not names:
        usage_error("--repos needs at least one repo name: sandbox SET up --repos NAME...")
    if names and not use_repos:
        usage_error(
            f"unexpected argument(s) {' '.join(names)}: did you mean --repos {' '.join(names)}?"
        )
    repos = names if use_repos else None
    try:
        config = load_config()
        template = (
            TemplateStore(config.templates_root).load(from_template)
            if from_template is not None
            else None
        )
        deps = get_deps(ctx)
        provisioner = Provisioner(config, deps.git, deps.compose, warn, images=deps.images)
        result = asyncio.run(
            provisioner.up(
                set_name,
                repos=repos,
                cwd=Path.cwd(),
                template=template,
                recreate=recreate,
            )
        )
    except SandboxError as exc:
        failure(exc)
    except KeyboardInterrupt:
        # The pipeline has already rolled the set's services back (and said so).
        typer.echo(f"error: interrupted: `up` of set {set_name!r} was cancelled", err=True)
        raise typer.Exit(code=130) from None

    typer.echo(f"set {result.set_name}: {result.set_dir} ({result.action})")
    typer.echo(f"  plan: {result.plan_file}{' (created)' if result.plan_created else ''}")
    for worktree in result.worktrees:
        detail = {
            WorktreeOutcome.CREATED: f"created from {worktree.base}",
            WorktreeOutcome.REUSED_BRANCH: "existing branch reused",
            WorktreeOutcome.EXISTING: "already present",
        }[worktree.outcome]
        typer.echo(f"  {worktree.repo}: {worktree.path} [{worktree.branch}, {detail}]")
    if result.services:
        status = "running" if result.action is UpAction.NOOP else "healthy"
        typer.echo(f"  services: {', '.join(result.services)} {status} [{result.project}]")
        if result.bindings:
            typer.echo("  bindings:")
            for line in format_bindings(result.bindings):
                typer.echo(f"    {line}")
    else:
        typer.echo("  services: none (no .sandbox/compose.yaml services)")
    typer.echo(f"  agent: running [{result.project}]; open a shell: sandbox {result.set_name} sh")


app.command()(down)
app.command()(save)
app.command()(templates)
app.command()(sh)
app.command()(claude)
app.command()(status)
app.command()(prune)


def main() -> None:
    """Console-script entry point."""
    try:
        ensure_supported_platform()
    except SandboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    app()
