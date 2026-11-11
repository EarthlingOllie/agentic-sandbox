"""What every CLI verb shares: injected dependencies, the leading SET, and error exits.

Verbs live in ``sandbox.commands.<verb>`` and are registered in ``sandbox.cli`` with one
``app.command`` line, so this module holds the helpers they need without importing the app.
"""

from dataclasses import dataclass, field
from typing import Final, NoReturn

import typer

from sandbox.agent_image import DockerImageProbe, ImageProbe
from sandbox.certs import Mkcert, SubprocessMkcert
from sandbox.compose.cli import Compose, SubprocessCompose
from sandbox.docker import Docker, SubprocessDocker
from sandbox.errors import SandboxError
from sandbox.git import Git, SubprocessGit

SET_META_KEY: Final = "sandbox.set"


@dataclass
class Deps:
    """External collaborators, swappable for fakes in tests."""

    git: Git = field(default_factory=SubprocessGit)
    compose: Compose = field(default_factory=SubprocessCompose)
    images: ImageProbe = field(default_factory=DockerImageProbe)
    docker: Docker = field(default_factory=SubprocessDocker)
    mkcert: Mkcert = field(default_factory=SubprocessMkcert)


def usage_error(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=2)


def failure(exc: SandboxError) -> NoReturn:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(code=1) from exc


def warn(message: str) -> None:
    typer.echo(f"warning: {message}", err=True)


def current_set(ctx: typer.Context, verb: str) -> str:
    name = ctx.meta.get(SET_META_KEY)
    if not isinstance(name, str):
        usage_error(f"{verb} needs a set: sandbox SET {verb}")
    return name


def get_deps(ctx: typer.Context) -> Deps:
    deps = ctx.obj
    if not isinstance(deps, Deps):
        raise TypeError("sandbox CLI context object must be a Deps instance")
    return deps
