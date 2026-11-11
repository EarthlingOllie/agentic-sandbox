"""A repo's backing services, read from ``<repo>/.sandbox/compose.yaml``.

Only the names sandbox needs are extracted here; Compose itself parses the full file. The
label fragment tags every service, network and volume the repo declares with the set name,
so the set's resources can later be reconciled against Docker. (Anonymous volumes cannot be
labelled by an override; Compose still tags them with the project label.)
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sandbox.compose import SET_LABEL
from sandbox.errors import SandboxError
from sandbox.yamlio import read_yaml

COMPOSE_FILENAME: Final = "compose.yaml"
SANDBOX_DIRNAME: Final = ".sandbox"


@dataclass(frozen=True)
class RepoCompose:
    repo: str
    file: Path
    services: tuple[str, ...]
    networks: tuple[str, ...]  # declared and not external
    volumes: tuple[str, ...]  # declared and not external


def compose_file(source: Path) -> Path:
    return source / SANDBOX_DIRNAME / COMPOSE_FILENAME


def load_repo_compose(repo: str, source: Path) -> RepoCompose | None:
    """Read the repo's compose file, or None when it has none (worktree-only repo)."""
    path = compose_file(source)
    if not path.exists():
        return None
    data = read_yaml(path)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SandboxError(f"{repo}: {path}: expected a mapping at the top level")
    document = _string_keys(data)  # pyright: ignore[reportUnknownArgumentType]
    return RepoCompose(
        repo=repo,
        file=path,
        services=tuple(_section(repo, path, document, "services")),
        networks=tuple(_owned(repo, path, document, "networks")),
        volumes=tuple(_owned(repo, path, document, "volumes")),
    )


def _section(repo: str, path: Path, document: dict[str, Any], key: str) -> dict[str, Any]:
    section = document.get(key)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SandboxError(f"{repo}: {path}: {key}: expected a mapping")
    return _string_keys(section)  # pyright: ignore[reportUnknownArgumentType]


def _string_keys(mapping: dict[Any, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in mapping.items()}


def _owned(repo: str, path: Path, document: dict[str, Any], key: str) -> list[str]:
    """Declared networks/volumes that Compose creates (so sandbox may label them)."""
    names: list[str] = []
    for name, spec in _section(repo, path, document, key).items():
        if isinstance(spec, dict) and spec.get("external"):  # pyright: ignore[reportUnknownMemberType]
            continue
        names.append(name)
    return names


def set_labels_fragment(set_name: str, repos: list[RepoCompose]) -> dict[str, Any]:
    labels = {SET_LABEL: set_name}
    services = sorted({s for r in repos for s in r.services})
    networks = sorted({"default", *(n for r in repos for n in r.networks)})
    volumes = sorted({v for r in repos for v in r.volumes})
    fragment: dict[str, Any] = {
        "services": {name: {"labels": dict(labels)} for name in services},
        "networks": {name: {"labels": dict(labels)} for name in networks},
    }
    if volumes:
        fragment["volumes"] = {name: {"labels": dict(labels)} for name in volumes}
    return fragment
