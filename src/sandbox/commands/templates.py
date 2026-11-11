"""``sandbox templates``: list the templates in the templates root."""

import typer

from sandbox.config import load_config
from sandbox.context import SET_META_KEY, failure, usage_error, warn
from sandbox.errors import SandboxError
from sandbox.templates import TemplateStore


def templates(ctx: typer.Context) -> None:
    """List the templates in the templates root, one name per line."""
    if SET_META_KEY in ctx.meta:
        usage_error("templates takes no set: sandbox templates")
    try:
        store = TemplateStore(load_config().templates_root)
    except SandboxError as exc:
        failure(exc)
    names = store.names()
    if not names:
        warn(f"no templates in {store.root}; create one with: sandbox [SET] save --to TEMPLATE")
    for name in names:
        typer.echo(name)
