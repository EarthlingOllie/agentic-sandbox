"""The per-set ``override.yaml``: assembled from fragments, one per concern.

Each fragment is a function of the :class:`OverrideContext` returning a partial Compose
document; :func:`render` deep-merges them in registration order. The override is passed to
Compose *after* the repos' own files, so the repo files keep resolving relative paths from
the repo while sandbox's additions win where they overlap. A later concern adds a module
under ``sandbox.compose`` and one line to ``FRAGMENTS``.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Final, cast

from sandbox.compose import project_name
from sandbox.compose.agent import agent_fragment, extra_services
from sandbox.compose.dedup import Override, ServiceGraph, relocation_fragment
from sandbox.compose.hooks import hooks_fragment
from sandbox.compose.network import network_fragment
from sandbox.compose.services import RepoCompose, set_labels_fragment
from sandbox.compose.web import WebApp, extra_hostnames, web_fragment
from sandbox.hooks import find_hooks
from sandbox.yamlio import dump_yaml

OVERRIDE_FILENAME: Final = "override.yaml"


@dataclass(frozen=True)
class OverrideContext:
    set_name: str
    set_dir: Path
    repos: list[RepoCompose]
    # The deduplicated services of all repos (see sandbox.compose.dedup).
    graph: ServiceGraph = field(default_factory=lambda: ServiceGraph({}))
    # Every repo in the plan (including ones without services) -> its absolute source.
    sources: Mapping[str, Path] = field(default_factory=dict[str, Path])
    # The plan's agent override, if any (see sandbox.compose.agent).
    agent: Mapping[str, Any] | None = None
    # Service name -> its leased loopback IP (see sandbox.leases).
    ips: Mapping[str, IPv4Address] = field(default_factory=dict[str, IPv4Address])
    # The repos' web apps, the mkcert CA certificate and the set's TLS directory
    # (see sandbox.web; both paths None without a web app).
    web_apps: Sequence[WebApp] = ()
    root_ca: Path | None = None
    tls_dir: Path | None = None


Fragment = Callable[[OverrideContext], dict[str, Any]]


def _project(ctx: OverrideContext) -> dict[str, Any]:
    return {"name": project_name(ctx.set_name)}


def _set_labels(ctx: OverrideContext) -> dict[str, Any]:
    return set_labels_fragment(ctx.set_name, ctx.repos)


def _relocate(ctx: OverrideContext) -> dict[str, Any]:
    return relocation_fragment(ctx.graph, ctx.repos)


def _agent(ctx: OverrideContext) -> dict[str, Any]:
    return agent_fragment(ctx.set_name, ctx.set_dir, ctx.sources, ctx.agent, ctx.root_ca)


def _network(ctx: OverrideContext) -> dict[str, Any]:
    definitions = {name: s.definition for name, s in ctx.graph.services.items()}
    return network_fragment(ctx.set_name, definitions, ctx.ips, extra_hostnames(ctx.web_apps))


def _init_hooks(ctx: OverrideContext) -> dict[str, Any]:
    return hooks_fragment(find_hooks(ctx.sources, ctx.graph.names()))


def _web(ctx: OverrideContext) -> dict[str, Any]:
    definitions = {name: s.definition for name, s in ctx.graph.services.items()}
    return web_fragment(
        ctx.web_apps, definitions, ctx.ips, ctx.root_ca, ctx.tls_dir, extra_services(ctx.agent)
    )


FRAGMENTS: Final[list[Fragment]] = [
    _project,
    _set_labels,
    _relocate,
    _agent,
    _network,
    _init_hooks,
    _web,
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
    # The agent mounts the set directory read-write: never follow what it may have planted
    # at the temporary name (a symlink would make this write any file the user owns).
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(dump_yaml(render(ctx)))
    temporary.replace(path)
    return path


def _merge(into: dict[str, Any], other: dict[str, Any]) -> None:
    for key, value in other.items():
        current = into.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value)  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(current, list) and isinstance(value, list):
            # Two fragments adding to one list (e.g. an init-hook mount and the CA mount on
            # the same service's volumes) both keep their entries.
            into[key] = [*cast(list[Any], current), *cast(list[Any], value)]
        elif isinstance(current, Override) and isinstance(current.value, list):
            # A restated (``!override``) list keeps its entries; later fragments add to it.
            into[key] = Override([*current.value, *cast(list[Any], value)])
        elif isinstance(current, Override) and isinstance(value, dict):
            # Likewise a restated mapping (a relocated service's environment, sysctls).
            merged = dict(current.value)
            _merge(merged, cast(dict[str, Any], value))
            into[key] = Override(merged)
        else:
            into[key] = value
