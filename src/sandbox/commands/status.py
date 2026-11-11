"""``sandbox [SET] status [--json]``: every set's state, or one set's services and repos."""

import asyncio
from collections.abc import Sequence
from typing import Annotated

import typer

from sandbox.config import load_config
from sandbox.context import SET_META_KEY, failure, get_deps
from sandbox.errors import SandboxError
from sandbox.git import SYNCED
from sandbox.models.status import RepoStatus, SetStatus, StatusList
from sandbox.status import StatusReader


def status(
    ctx: typer.Context,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the same facts as JSON, for scripts.")
    ] = False,
) -> None:
    """List every set with its state, or (with SET) its services, hostnames, IPs and repos.

    States: running, stopped, partial (some services down), orphaned (containers or an IP
    lease left behind without a set plan; `sandbox prune` clears them).
    """
    set_name = ctx.meta.get(SET_META_KEY)
    try:
        deps = get_deps(ctx)
        reader = StatusReader(load_config(), deps.git, deps.docker)
        if isinstance(set_name, str):
            detail = asyncio.run(reader.set_status(set_name))
            typer.echo(detail.model_dump_json(indent=2) if as_json else render_set(detail))
        else:
            listing = asyncio.run(reader.list_sets())
            typer.echo(listing.model_dump_json(indent=2) if as_json else render_list(listing))
    except SandboxError as exc:
        failure(exc)


def render_list(listing: StatusList) -> str:
    if not listing.sets:
        return "no sets (create one with: sandbox SET up --repos NAME...)"
    return _table(
        ["SET", "STATE", "DETAIL"],
        [[entry.name, entry.state, entry.detail] for entry in listing.sets],
    )


def render_set(detail: SetStatus) -> str:
    heading = f"set {detail.name}: {detail.state} [{detail.project}]"
    if detail.detail:
        heading += f" ({detail.detail})"
    lines = [heading, ""]
    lines.append(
        _table(
            ["SERVICE", "STATE", "HEALTH", "HOSTNAME", "IP"],
            [
                [s.name, s.state, s.health or "-", s.hostname or "-", str(s.ip or "-")]
                for s in detail.services
            ],
        )
    )
    if detail.repos:
        lines.append("")
        lines.append(
            _table(
                ["REPO", "BRANCH", "CHANGES"],
                [[r.name, _branch(r), _changes(r)] for r in detail.repos],
            )
        )
        lines.extend(
            f"{r.name}: agent branch not synced into {r.name}'s repo ({r.sync}): {r.sync_detail}"
            for r in detail.repos
            if r.sync not in SYNCED
        )
    return "\n".join(lines)


def _branch(repo: RepoStatus) -> str:
    if not repo.exists:
        return "-"
    return repo.branch or "(detached)"


def _changes(repo: RepoStatus) -> str:
    if not repo.exists:
        return f"clone missing: {repo.path}"
    marks: list[str] = []
    if repo.modified:
        marks.append("* uncommitted changes")
    if repo.untracked:
        marks.append("? untracked files")
    return ", ".join(marks) or "clean"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in [headers, *rows]
    )
