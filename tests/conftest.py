"""Shared fixtures: the primary test seam is the CLI invoked in-process.

Every host-state location sandbox uses is redirected into ``tmp_path`` through a config
file named by ``SANDBOX_CONFIG``, so tests never touch the real home directory.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from sandbox.certs import Mkcert
from sandbox.cli import app
from sandbox.compose.cli import Compose
from sandbox.context import Deps
from sandbox.docker import Docker, SubprocessDocker
from sandbox.git import Git
from sandbox.platforms import Host
from tests.fakes_compose import FakeCompose
from tests.fakes_docker import FakeDocker
from tests.fakes_host import point_at_resolver


@dataclass
class Env:
    root: Path
    cwd: Path
    config_file: Path
    sets_root: Path
    templates_root: Path
    state_dir: Path

    def set_dir(self, name: str) -> Path:
        return self.sets_root / name

    def make_repo_dir(self, name: str, *, opt_in: bool = True) -> Path:
        path = self.cwd / name
        path.mkdir(parents=True)
        if opt_in:
            (path / ".sandbox").mkdir()
        return path


@pytest.fixture(autouse=True)
def _linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests see a Linux host unless they pass a ``Host`` (``tests.fakes_host``), so a unit
    run on a Mac never reads that Mac's setup state."""
    monkeypatch.setattr("sandbox.platforms.current_platform", lambda: "linux")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    root = tmp_path.resolve()  # macOS: /var is a symlink to /private/var
    home = root / "home"
    home.mkdir()
    cwd = root / "code"
    cwd.mkdir()
    environment = Env(
        root=root,
        cwd=cwd,
        config_file=root / "config.yaml",
        sets_root=root / "sets",
        templates_root=root / "plans",
        state_dir=root / "state",
    )
    environment.config_file.write_text(
        f"sets_root: {environment.sets_root}\n"
        f"templates_root: {environment.templates_root}\n"
        f"state_dir: {environment.state_dir}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("SANDBOX_CONFIG", str(environment.config_file))
    monkeypatch.chdir(cwd)
    return environment


def set_up_machine() -> Host:
    """The real machine, except that its system files live in a directory beside the test's
    config, where ``sudo sandbox setup`` has pointed it at the resolver for whichever domain
    that config names. Tests of setup and of ``up``'s prerequisite checks pass a host of
    their own (``tests.fakes_host``)."""
    config_file = Path(os.environ["SANDBOX_CONFIG"])
    try:
        config = yaml.safe_load(config_file.read_text())
        domain = config["domain"]
    except (OSError, yaml.YAMLError, TypeError, KeyError):
        domain = "test"
    root = config_file.parent / "set-up-machine"
    point_at_resolver(root, str(domain))
    return Host(root=root)


def invoke(
    args: Sequence[str],
    git: Git,
    compose: Compose | None = None,
    docker: Docker | None = None,
    mkcert: Mkcert | None = None,
    host: Host | None = None,
) -> Result:
    """Run the CLI in-process. Compose defaults to a fake so git-only tests need no Docker;
    Docker queries default to a fake that reports what that Compose fake is running; mkcert
    defaults to the real CLI (only sets with a web app use it); the host defaults to a
    machine setup has run on (:func:`set_up_machine`)."""
    compose = compose if compose is not None else FakeCompose()
    if docker is None:
        docker = FakeDocker(compose) if isinstance(compose, FakeCompose) else SubprocessDocker()
    deps = Deps(git=git, compose=compose, docker=docker)
    if mkcert is not None:
        deps.mkcert = mkcert
    deps.host = host if host is not None else set_up_machine()
    return CliRunner().invoke(app, list(args), obj=deps)
