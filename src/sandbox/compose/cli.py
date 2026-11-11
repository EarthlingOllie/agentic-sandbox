"""The Compose CLI seam: ``docker compose`` driven via subprocess, never the Docker SDK.

Unit tests substitute ``tests.fakes_compose.FakeCompose`` for :class:`SubprocessCompose`.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from sandbox.errors import SandboxError


class ComposeError(SandboxError):
    pass


class Compose(Protocol):
    async def up(self, project: str, files: list[Path]) -> None:
        """Start ``project`` from ``files`` (merged in order) and wait until every service
        is healthy (or running, for services without a healthcheck)."""
        ...

    async def down(self, project: str) -> None:
        """Stop and remove the project's containers, networks and volumes."""
        ...

    async def running_services(self, project: str) -> set[str]:
        """Names of the project's services that have a running container."""
        ...


class SubprocessCompose:
    """Runs the real ``docker compose`` CLI."""

    async def _check(self, args: Sequence[str]) -> str:
        argv = ["docker", "compose", *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ComposeError("docker is not installed or not on PATH") from exc
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            # Don't leave `compose up` creating containers behind a rollback's `down`.
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            raise
        if process.returncode != 0:
            output = (stderr.decode(errors="replace") + stdout.decode(errors="replace")).strip()
            raise ComposeError(f"{' '.join(argv)} failed:\n{output}")
        return stdout.decode(errors="replace")

    async def up(self, project: str, files: list[Path]) -> None:
        file_args = [arg for path in files for arg in ("-f", str(path))]
        await self._check(["-p", project, *file_args, "up", "-d", "--wait"])

    async def down(self, project: str) -> None:
        # By project name alone, Compose finds every resource through its project label, so
        # `down` works even if a repo's compose file changed or disappeared since `up`.
        await self._check(["-p", project, "down", "--volumes", "--remove-orphans"])

    async def running_services(self, project: str) -> set[str]:
        output = await self._check(["-p", project, "ps", "--services", "--status", "running"])
        return {line.strip() for line in output.splitlines() if line.strip()}
