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

    async def up(self, project: str, files: list[Path]) -> None:
        self.calls.append(("up", project))
        for path in files:
            if not path.is_file():
                raise ComposeError(f"open {path}: no such file or directory")
            yaml.safe_load(path.read_text())
        if self.up_error is not None:
            raise ComposeError(self.up_error)
        self.running[project] = list(files)

    async def down(self, project: str) -> None:
        self.calls.append(("down", project))
        self.running.pop(project, None)
