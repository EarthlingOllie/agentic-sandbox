"""In-memory stand-in for the Docker Compose CLI, used by unit tests behind the CLI seam.

It checks what the real CLI would trip over (every ``-f`` file must exist and be YAML) and
records each project's files, so tests can assert on what sandbox handed to Compose.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sandbox.compose.cli import ComposeError


@dataclass
class FakeCompose:
    # project name -> the -f files it was started with, in order
    running: dict[str, list[Path]] = field(default_factory=dict[str, list[Path]])
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    # When set, `up` fails with this message, as `compose up --wait` does for an
    # unhealthy service.
    up_error: str | None = None
    # When set, `down` fails with this message.
    down_error: str | None = None
    # project -> services whose containers have stopped (they no longer count as running)
    stopped_services: dict[str, set[str]] = field(default_factory=dict[str, set[str]])

    async def up(self, project: str, files: list[Path]) -> None:
        self.calls.append(("up", project))
        for path in files:
            if not path.is_file():
                raise ComposeError(f"open {path}: no such file or directory")
            yaml.safe_load(path.read_text())
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
            document: dict[str, dict[str, object] | None] = yaml.safe_load(path.read_text()) or {}
            services.update(document.get("services") or {})
        return services - self.stopped_services.get(project, set())
