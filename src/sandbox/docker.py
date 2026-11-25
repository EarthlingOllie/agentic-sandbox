"""What sandbox asks Docker outside a set's Compose project, via the ``docker`` CLI.

Every container sandbox starts for a set carries the ``sandbox.set=<set>`` label
(``sandbox.compose``), so one ``docker ps`` over that label finds every set's containers,
including leftovers whose set directory is gone. The resolver (``sandbox.compose.resolver``)
is started from its bundled Compose project and attached to and detached from set networks
here. Unit tests substitute ``tests.fakes_docker.FakeDocker``.
"""

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, cast

from sandbox.compose import SET_LABEL
from sandbox.compose.resolver import (
    COMPOSE_FILE,
    RESOLVER_CONTAINER,
    RESOLVER_PROJECT,
    resolver_environment,
)
from sandbox.errors import SandboxError

SERVICE_LABEL: Final = "com.docker.compose.service"
# The throwaway container that deletes root-owned files runs this image: the default agent
# image is built from it, so it is already on any machine that has run a set.
CLEANUP_IMAGE: Final = "ubuntu:24.04"
_CLEANUP_MOUNT: Final = "/sandbox-cleanup"


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

    async def empty_as_root(self, path: Path) -> None:
        """Delete everything inside the directory ``path`` (keeping ``path`` itself) from a
        throwaway root container, for files containers left behind owned by root."""
        ...

    async def ensure_resolver(self, domain: str) -> None:
        """Start the resolver, forwarding ``domain``, unless it already runs that way; its
        image is built from the bundled definition when missing."""
        ...

    async def attach_resolver(self, network: str) -> None:
        """Put the running resolver on ``network``; a no-op when it is already there."""
        ...

    async def detach_resolver(self, network: str) -> None:
        """Take the resolver off ``network``; a no-op when it is not there (or not running)."""
        ...


class SubprocessDocker:
    """Runs the real ``docker`` CLI."""

    async def _check(self, args: Sequence[str], env: Mapping[str, str] | None = None) -> str:
        argv = ["docker", *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **env} if env is not None else None,
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

    async def empty_as_root(self, path: Path) -> None:
        await self._check(
            [
                "run", "--rm", "--pull", "missing", "--network", "none",
                "--user", "0:0", "--entrypoint", "find",
                "--volume", f"{path.resolve()}:{_CLEANUP_MOUNT}",
                CLEANUP_IMAGE, _CLEANUP_MOUNT, "-mindepth", "1", "-delete",
            ]
        )  # fmt: skip

    async def ensure_resolver(self, domain: str) -> None:
        # Compose leaves a container that already matches alone, builds the image when it
        # is missing and recreates the container when the domain (its command) changed.
        await self._check(
            ["compose", "-p", RESOLVER_PROJECT, "-f", str(COMPOSE_FILE), "up", "-d", "--wait"],
            env=resolver_environment(domain),
        )

    async def attach_resolver(self, network: str) -> None:
        attached = await self._resolver_networks()
        if attached is None:
            raise DockerError(f"the resolver ({RESOLVER_CONTAINER}) is not running")
        if network in attached:
            return
        try:
            await self._check(["network", "connect", network, RESOLVER_CONTAINER])
        except DockerError:
            # Another `up` of the same set may have attached it meanwhile.
            if network not in (await self._resolver_networks() or ()):
                raise

    async def detach_resolver(self, network: str) -> None:
        if network not in (await self._resolver_networks() or ()):
            return
        try:
            await self._check(["network", "disconnect", network, RESOLVER_CONTAINER])
        except DockerError:
            if network in (await self._resolver_networks() or ()):
                raise

    async def _resolver_networks(self) -> set[str] | None:
        """The networks the resolver's container is on; None when there is no such container."""
        listed = await self._check(
            ["ps", "--all", "--quiet", "--filter", f"name=^{RESOLVER_CONTAINER}$"]
        )
        if not listed.split():
            return None
        try:
            output = await self._check(
                ["container", "inspect", "--format", "{{json .NetworkSettings.Networks}}",
                 RESOLVER_CONTAINER]
            )  # fmt: skip
        except DockerError:
            return None  # removed since the listing
        try:
            networks = cast(dict[str, Any] | None, json.loads(output))
        except json.JSONDecodeError as exc:
            raise DockerError(f"docker container inspect printed invalid JSON: {exc}") from exc
        return set(networks or {})


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
