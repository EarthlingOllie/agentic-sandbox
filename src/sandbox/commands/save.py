"""``sandbox [SET] save --to TEMPLATE [--repos NAME...]``: save a plan as a template.

With SET, the set's plan (bases included) becomes the template. Without SET, ``--repos``
builds a template from repo directories in the current directory, pinning absolute sources
and leaving ``base`` out so ``up --from`` pins whatever branch is checked out at the time.
"""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from sandbox.config import load_config
from sandbox.context import SET_META_KEY, failure, get_deps, usage_error, warn
from sandbox.errors import SandboxError
from sandbox.plan import TemplatePlan, load_set_plan, plan_path
from sandbox.provisioner import Provisioner
from sandbox.templates import TemplateStore, validate_template_name


def save(
    ctx: typer.Context,
    to: Annotated[
        str,
        typer.Option("--to", metavar="TEMPLATE", help="Name of the template to write."),
    ],
    use_repos: Annotated[
        bool,
        typer.Option(
            "--repos",
            help="Build the template from the NAME directories in the current directory.",
        ),
    ] = False,
    names: Annotated[
        list[str] | None,
        typer.Argument(metavar="NAME...", show_default=False),
    ] = None,
) -> None:
    """Save a set's plan, or repos in the current directory, as a template."""
    set_name = ctx.meta.get(SET_META_KEY)
    if use_repos and not names:
        usage_error(
            "--repos needs at least one repo name: sandbox save --to TEMPLATE --repos NAME..."
        )
    if names and not use_repos:
        usage_error(
            f"unexpected argument(s) {' '.join(names)}: did you mean --repos {' '.join(names)}?"
        )
    if isinstance(set_name, str) and use_repos:
        usage_error("save takes either a SET or --repos, not both")
    if not isinstance(set_name, str) and not use_repos:
        usage_error(
            "save needs a source: sandbox SET save --to TEMPLATE, "
            "or sandbox save --to TEMPLATE --repos NAME..."
        )
    try:
        validate_template_name(to)
        config = load_config()
        if isinstance(set_name, str):
            plan = _set_plan(config.sets_root / set_name, set_name)
        else:
            deps = get_deps(ctx)
            provisioner = Provisioner(config, deps.git, deps.compose, warn)
            plan = asyncio.run(provisioner.template_from_directory(Path.cwd(), names or []))
        path = TemplateStore(config.templates_root).save(to, plan)
    except SandboxError as exc:
        failure(exc)
    typer.echo(f"template {to}: {path}")


def _set_plan(set_dir: Path, set_name: str) -> TemplatePlan:
    if not plan_path(set_dir).exists():
        raise SandboxError(f"no set {set_name!r}: {plan_path(set_dir)} does not exist")
    plan = load_set_plan(set_dir)
    return TemplatePlan.model_validate(plan.model_dump(mode="json"))
