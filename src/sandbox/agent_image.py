"""Checking a custom agent image: ``sh``, ``git`` and ``claude`` must be on its PATH.

The probe runs the image once with the ``docker`` CLI (never the Docker SDK), as the user
the agent will run as, with no network, and asks the image's own ``sh`` which commands it
cannot find. Unit tests substitute ``tests.fakes_images.FakeImageProbe``.
"""

import asyncio
from collections.abc import Sequence
from typing import Final, Protocol

from sandbox.compose.agent import agent_user
from sandbox.errors import SandboxError

REQUIRED_COMMANDS: Final = ("sh", "git", "claude")
# `docker run` exits 126/127 when the entrypoint (here: `sh`) can't be executed or found.
_ENTRYPOINT_MISSING: Final = frozenset({126, 127})
_PROBE_SCRIPT: Final = 'for c in "$@"; do command -v "$c" >/dev/null 2>&1 || echo "$c"; done'


class ImageError(SandboxError):
    pass


class ImageProbe(Protocol):
    async def missing_commands(self, image: str, commands: Sequence[str]) -> list[str]:
        """The ``commands`` that ``image`` lacks on its PATH (pulling it if missing).

        Raises :class:`ImageError` if the image can't be pulled or run at all.
        """
        ...


class DockerImageProbe:
    """Runs the image with the real ``docker`` CLI."""

    async def missing_commands(self, image: str, commands: Sequence[str]) -> list[str]:
        argv = [
            "docker", "run", "--rm", "--pull", "missing", "--network", "none",
            "--user", agent_user(), "--entrypoint", "sh",
            image, "-c", _PROBE_SCRIPT, "sh", *commands,
        ]  # fmt: skip
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ImageError("docker is not installed or not on PATH") from exc
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            raise
        if process.returncode in _ENTRYPOINT_MISSING:
            return ["sh"]  # without `sh` nothing else can be checked
        if process.returncode != 0:
            output = (stderr.decode(errors="replace") + stdout.decode(errors="replace")).strip()
            raise ImageError(f"cannot run agent image {image!r}:\n{output}")
        reported = {line.strip() for line in stdout.decode(errors="replace").splitlines()}
        return [command for command in commands if command in reported]
