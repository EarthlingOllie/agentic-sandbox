"""Service deduplication: the repos of a set share one instance of each same-named service.

Each repo's compose file is normalised on its own with ``docker compose config`` (so
``extends``, relative paths and interpolation are resolved per repo), then services are
matched **by name only**. Same-named definitions must be identical after normalisation,
except that:

- ``depends_on``, ``labels``, ``ports`` and ``x-sandbox`` are ignored (sandbox rewrites ports),
  as are init-hook mounts under ``/sandbox/init/`` (one per repo by design);
- a ``healthcheck`` set by only one repo is kept; two different healthchecks conflict.

Any other difference fails ``up`` before a container starts, with a diff naming both repos.
Multi-version services (``mysql84`` beside ``mysql``) are not supported in the MVP.

Compose is then given every repo's original file plus the override. Compose resolves the
relative paths of *all* those files from the first file's directory (the project directory),
so :func:`relocation_fragment` restates, with Compose's ``!override`` merge tag, the
normalised definition of each service the first repo does not declare.
"""

import asyncio
import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import yaml

from sandbox.compose.cli import Compose, ComposeError
from sandbox.compose.services import RepoCompose
from sandbox.errors import SandboxError

IGNORED_KEYS: Final = frozenset({"depends_on", "labels", "ports", "x-sandbox"})
HEALTHCHECK: Final = "healthcheck"
# Where sandbox mounts init hooks (see sandbox.hooks); such mounts never conflict.
INIT_MOUNT_ROOT: Final = "/sandbox/init/"


class ServiceConflictError(SandboxError):
    pass


@dataclass(frozen=True)
class SharedService:
    name: str
    # Repos declaring the service, in repo-name order.
    repos: tuple[str, ...]
    # The normalised definition (from the first declaring repo, plus a healthcheck that
    # only a later repo sets).
    definition: dict[str, Any]


@dataclass(frozen=True)
class ServiceGraph:
    services: dict[str, SharedService]

    def names(self) -> list[str]:
        return sorted(self.services)


async def resolve(compose: Compose, project: str, repos: Sequence[RepoCompose]) -> ServiceGraph:
    """Normalise each repo's compose file on its own, then merge services by name."""
    with_services = sorted((r for r in repos if r.services), key=lambda r: r.repo)
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(_normalise(compose, project, r)) for r in with_services]
    except* SandboxError as failed:
        raise SandboxError("\n".join(str(e) for e in failed.exceptions)) from failed
    return merge([(r.repo, task.result()) for r, task in zip(with_services, tasks, strict=True)])


def merge(normalised: Sequence[tuple[str, Mapping[str, Mapping[str, Any]]]]) -> ServiceGraph:
    """Merge ``(repo, services)`` pairs into one graph, or raise with every conflict."""
    merged: dict[str, SharedService] = {}
    conflicts: list[str] = []
    for repo, services in sorted(normalised, key=lambda pair: pair[0]):
        for name, spec in services.items():
            current = merged.get(name)
            if current is None:
                merged[name] = SharedService(name, (repo,), dict(spec))
                continue
            difference = _difference(name, current.repos[0], current.definition, repo, spec)
            if difference:
                conflicts.append(difference)
                continue
            definition = dict(current.definition)
            if HEALTHCHECK not in definition and HEALTHCHECK in spec:
                definition[HEALTHCHECK] = spec[HEALTHCHECK]
            merged[name] = SharedService(name, (*current.repos, repo), definition)
    if conflicts:
        raise ServiceConflictError("\n".join(conflicts))
    return ServiceGraph(dict(sorted(merged.items())))


def relocation_fragment(graph: ServiceGraph, repos: Sequence[RepoCompose]) -> dict[str, Any]:
    """Override entries for services the project-directory repo (the first file) lacks.

    ``repos`` is in the order their files are passed to Compose.
    """
    if not repos:
        return {}
    first = repos[0].repo
    services: dict[str, Any] = {}
    for name, service in graph.services.items():
        if first in service.repos:
            continue
        restated: dict[str, Any] = {
            key: Override(cast(Any, value)) if isinstance(value, Mapping | list) else value
            for key, value in service.definition.items()
            if key not in IGNORED_KEYS and not key.startswith("x-") and value is not None
        }
        # `config` already inlined env files into `environment`; drop the unresolved paths.
        restated["env_file"] = Reset()
        services[name] = restated
    return {"services": services} if services else {}


@dataclass(frozen=True)
class Override:
    """A mapping or list rendered with Compose's ``!override`` tag: replaces, never merges."""

    value: Mapping[str, Any] | list[Any]


@dataclass(frozen=True)
class Reset:
    """Compose's ``!reset`` tag: removes whatever earlier files set."""


def _represent_override(dumper: yaml.SafeDumper, data: Override) -> yaml.Node:
    if isinstance(data.value, Mapping):
        return dumper.represent_mapping("!override", data.value)  # pyright: ignore[reportUnknownArgumentType]
    return dumper.represent_sequence("!override", data.value)  # pyright: ignore[reportUnknownArgumentType]


def _represent_reset(dumper: yaml.SafeDumper, _data: Reset) -> yaml.Node:
    return dumper.represent_scalar("!reset", "")  # pyright: ignore[reportUnknownMemberType]


yaml.SafeDumper.add_representer(Override, _represent_override)
yaml.SafeDumper.add_representer(Reset, _represent_reset)


async def _normalise(
    compose: Compose, project: str, repo: RepoCompose
) -> dict[str, Mapping[str, Any]]:
    try:
        document = await compose.config(project, [repo.file])
    except ComposeError as exc:
        raise SandboxError(f"{repo.repo}: {exc}") from exc
    services: object = document.get("services") or {}
    if not isinstance(services, dict):
        raise SandboxError(f"{repo.repo}: docker compose config: services is not a mapping")
    return {str(name): spec or {} for name, spec in cast(dict[Any, Any], services).items()}


def _difference(
    name: str,
    first_repo: str,
    first: Mapping[str, Any],
    other_repo: str,
    other: Mapping[str, Any],
) -> str | None:
    left, right = _comparable(first), _comparable(other)
    if HEALTHCHECK not in left or HEALTHCHECK not in right:
        left.pop(HEALTHCHECK, None)
        right.pop(HEALTHCHECK, None)
    if left == right:
        return None
    diff = difflib.unified_diff(
        _dump(left),
        _dump(right),
        fromfile=f"{first_repo}: {name}",
        tofile=f"{other_repo}: {name}",
        lineterm="",
    )
    body = "\n".join(f"  {line}" for line in diff)
    return f"service {name!r} is defined differently by {first_repo} and {other_repo}:\n{body}"


def _comparable(spec: Mapping[str, Any]) -> dict[str, Any]:
    comparable = {key: value for key, value in spec.items() if key not in IGNORED_KEYS}
    if isinstance(volumes := comparable.get("volumes"), list):
        comparable["volumes"] = [v for v in cast(list[Any], volumes) if not _init_mount(v)]
    return comparable


def _init_mount(volume: object) -> bool:
    """An init-hook mount (``/sandbox/init/<repo>``): per repo by design, never a conflict."""
    target = volume.get("target") if isinstance(volume, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return isinstance(target, str) and target.startswith(INIT_MOUNT_ROOT)


def _dump(spec: Mapping[str, Any]) -> list[str]:
    return yaml.safe_dump(dict(spec), sort_keys=True, default_flow_style=False).splitlines()
