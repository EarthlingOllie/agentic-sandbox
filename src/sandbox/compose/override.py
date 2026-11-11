"""The per-set ``override.yaml``: assembled from fragments, one per concern.

Each fragment is a function of the :class:`OverrideContext` returning a partial Compose
document; :func:`render` deep-merges them in registration order. The override is passed to
Compose *after* the repos' own files, so the repo files keep resolving relative paths from
the repo while sandbox's additions win where they overlap. A later concern adds a module
under ``sandbox.compose`` and one line to ``FRAGMENTS``.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from sandbox.compose import project_name
from sandbox.compose.agent import agent_fragment
from sandbox.compose.dedup import ServiceGraph, relocation_fragment
from sandbox.compose.services import RepoCompose, set_labels_fragment
from sandbox.yamlio import dump_yaml

OVERRIDE_FILENAME: Final = "override.yaml"


@dataclass(frozen=True)
class OverrideContext:
    set_name: str
    set_dir: Path
    repos: list[RepoCompose]
    # The deduplicated services of all repos (see sandbox.compose.dedup).
    graph: ServiceGraph = field(default_factory=lambda: ServiceGraph({}))
    # Every repo in the plan (including worktree-only ones) -> its absolute source.
    sources: Mapping[str, Path] = field(default_factory=dict[str, Path])


Fragment = Callable[[OverrideContext], dict[str, Any]]


def _project(ctx: OverrideContext) -> dict[str, Any]:
    return {"name": project_name(ctx.set_name)}


def _set_labels(ctx: OverrideContext) -> dict[str, Any]:
    return set_labels_fragment(ctx.set_name, ctx.repos)


def _relocate(ctx: OverrideContext) -> dict[str, Any]:
    return relocation_fragment(ctx.graph, ctx.repos)


def _agent(ctx: OverrideContext) -> dict[str, Any]:
    return agent_fragment(ctx.set_name, ctx.set_dir, ctx.sources)


FRAGMENTS: Final[list[Fragment]] = [
    _project,
    _set_labels,
    _relocate,
    _agent,
]


def render(ctx: OverrideContext) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for fragment in FRAGMENTS:
        _merge(document, fragment(ctx))
    return document


def write_override(ctx: OverrideContext) -> Path:
    """Render the override into the set directory (rewritten on every ``up``)."""
    path = ctx.set_dir / OVERRIDE_FILENAME
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(dump_yaml(render(ctx)), encoding="utf-8")
    temporary.replace(path)
    return path


def _merge(into: dict[str, Any], other: dict[str, Any]) -> None:
    for key, value in other.items():
        current = into.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value)  # pyright: ignore[reportUnknownArgumentType]
        else:
            into[key] = value
