"""In-memory stand-in for the Docker Compose CLI, used by unit tests behind the CLI seam.

It checks what the real CLI would trip over (every ``-f`` file must exist and be YAML) and
records each project's files, so tests can assert on what sandbox handed to Compose.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sandbox.compose.cli import ComposeError


class _TagTolerantLoader(yaml.SafeLoader):
    """Loads Compose's merge tags (``!override``, ``!reset``) as their plain values."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    return None


_TagTolerantLoader.add_multi_constructor("!", _untagged)  # pyright: ignore[reportUnknownMemberType]


def load_compose_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.load(path.read_text(), Loader=_TagTolerantLoader)  # noqa: S506
    return loaded if isinstance(loaded, dict) else {}  # pyright: ignore[reportUnknownVariableType]


@dataclass
class FakeCompose:
    # project name -> the -f files it was started with, in order
    running: dict[str, list[Path]] = field(default_factory=dict[str, list[Path]])
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    # The -f files of each `config` call, in call order (read-only, so not in `calls`).
    configured: list[list[Path]] = field(default_factory=list[list[Path]])
    # When set, `up` fails with this message, as `compose up --wait` does for an
    # unhealthy service.
    up_error: str | None = None
    # When set, `down` fails with this message.
    down_error: str | None = None
    # project -> services whose containers have stopped (they no longer count as running)
    stopped_services: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    # When set, `config` fails with this message, as it does for an invalid compose file.
    config_error: str | None = None

    async def up(self, project: str, files: list[Path]) -> None:
        self.calls.append(("up", project))
        for path in files:
            if not path.is_file():
                raise ComposeError(f"open {path}: no such file or directory")
            load_compose_yaml(path)
        if self.up_error is not None:
            raise ComposeError(self.up_error)
        self.running[project] = list(files)
        self.stopped_services.pop(project, None)

    async def down(self, project: str) -> None:
        self.calls.append(("down", project))
        if self.down_error is not None:
            raise ComposeError(self.down_error)
        self.running.pop(project, None)
        self.stopped_services.pop(project, None)

    async def running_services(self, project: str) -> set[str]:
        services: set[str] = set()
        for path in self.running.get(project, []):
            services.update(load_compose_yaml(path).get("services") or {})
        return services - self.stopped_services.get(project, set())

    async def config(self, project: str, files: list[Path]) -> dict[str, Any]:
        """Return the files merged in order: real normalisation (``extends``, path
        resolution, interpolation) is left to the integration tests."""
        self.configured.append(list(files))
        if self.config_error is not None:
            raise ComposeError(self.config_error)
        document: dict[str, Any] = {"name": project}
        for path in files:
            if not path.is_file():
                raise ComposeError(f"open {path}: no such file or directory")
            services: dict[str, Any] = load_compose_yaml(path).get("services") or {}
            for service, spec in services.items():
                document.setdefault("services", {}).setdefault(service, {}).update(spec)
        return document
