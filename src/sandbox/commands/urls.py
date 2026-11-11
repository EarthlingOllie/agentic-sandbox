"""``sandbox SET urls [--json]``: the set's web app URLs and service addresses, with IPs."""

import asyncio
from collections.abc import Sequence
from typing import Annotated

import typer

from sandbox.config import load_config
from sandbox.context import current_set, failure, get_deps
from sandbox.errors import SandboxError
from sandbox.models.urls import SetUrls
from sandbox.urls import UrlsReader


def urls(
    ctx: typer.Context,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the same addresses as JSON, for scripts.")
    ] = False,
) -> None:
    """List the set's web apps as https:// URLs and its services as host:port, with IPs.

    A stopped set lists the addresses from its sticky IP lease, marked as not running.
    """
    set_name = current_set(ctx, "urls")
    try:
        deps = get_deps(ctx)
        reader = UrlsReader(load_config(), deps.git, deps.compose, deps.docker)
        found = asyncio.run(reader.set_urls(set_name))
    except SandboxError as exc:
        failure(exc)
    typer.echo(found.model_dump_json(indent=2) if as_json else render_urls(found))


def render_urls(found: SetUrls) -> str:
    heading = f"set {found.name}: {found.state}"
    if not found.running:
        heading += " (not running; addresses from its sticky IP lease)"
    lines = [heading]
    if found.web:
        lines += ["", _table(["WEB", "IP"], [[w.url, _ip(w.ip)] for w in found.web])]
    if found.services:
        lines += [
            "",
            _table(["SERVICE", "IP"], [[s.address, _ip(s.ip)] for s in found.services]),
        ]
    if not found.web and not found.services:
        lines += ["", "no web apps or services (no .sandbox/compose.yaml services)"]
    return "\n".join(lines)


def _ip(ip: object) -> str:
    return "-" if ip is None else str(ip)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in [headers, *rows]
    )
