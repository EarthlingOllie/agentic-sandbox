"""Docker label queries: which containers exist for which set, via the ``docker`` CLI.

Every container sandbox starts carries the ``sandbox.set=<set>`` label (``sandbox.compose``),
so one ``docker ps`` over that label finds every set's containers, including leftovers whose
set directory is gone. Unit tests substitute ``tests.fakes_docker.FakeDocker``.
"""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from sandbox.compose import SET_LABEL
from sandbox.errors import SandboxError

SERVICE_LABEL: Final = "com.docker.compose.service"


class DockerError(SandboxError):
    pass


@dataclass(frozen=True)
class Container:
    set_name: str
    service: str
    state: str  # Docker's state: created, running, paused, restarting, exited, dead
    health: str | None  # healthy, unhealthy, starting; None without a healthcheck


class Docker(Protocol):
    async def set_containers(self) -> list[Container]:
        """Every container (running or not) labelled with a set."""
        ...


class SubprocessDocker:
    """Runs the real ``docker`` CLI."""

    async def _check(self, args: Sequence[str]) -> str:
        argv = ["docker", *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DockerError("docker is not installed or not on PATH") from exc
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            output = (stderr.decode(errors="replace") + stdout.decode(errors="replace")).strip()
            raise DockerError(f"{' '.join(argv)} failed:\n{output}")
        return stdout.decode(errors="replace")

    async def set_containers(self) -> list[Container]:
        # A container removed between `ps` and `inspect` fails the inspect; ask again, once.
        for attempt in (1, 2):
            listed = await self._check(["ps", "--all", "--quiet", "--filter", f"label={SET_LABEL}"])
            ids = listed.split()
            if not ids:
                return []
            try:
                output = await self._check(["container", "inspect", *ids])
            except DockerError:
                if attempt == 2:
                    raise
                continue
            try:
                documents = cast(list[dict[str, Any]], json.loads(output))
            except json.JSONDecodeError as exc:
                raise DockerError(f"docker container inspect printed invalid JSON: {exc}") from exc
            return sorted(map(_container, documents), key=lambda c: (c.set_name, c.service))
        return []


def _container(document: dict[str, Any]) -> Container:
    config = cast(dict[str, Any], document.get("Config") or {})
    labels = cast(dict[str, str], config.get("Labels") or {})
    state = cast(dict[str, Any], document.get("State") or {})
    health = cast(dict[str, Any] | None, state.get("Health"))
    name = str(document.get("Name") or "").lstrip("/")
    return Container(
        set_name=labels.get(SET_LABEL, ""),
        service=labels.get(SERVICE_LABEL) or name,
        state=str(state.get("Status") or "unknown"),
        health=str(health["Status"]) if health and health.get("Status") else None,
    )
