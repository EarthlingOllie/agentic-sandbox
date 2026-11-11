"""Shared fixtures: the primary test seam is the CLI invoked in-process.

Every host-state location sandbox uses is redirected into ``tmp_path`` through a config
file named by ``SANDBOX_CONFIG``, so tests never touch the real home directory.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from sandbox.cli import app
from sandbox.compose.cli import Compose
from sandbox.context import Deps
from sandbox.docker import Docker, SubprocessDocker
from sandbox.git import Git
from tests.fakes_compose import FakeCompose
from tests.fakes_docker import FakeDocker


@dataclass
class Env:
    root: Path
    cwd: Path
    config_file: Path
    sets_root: Path
    templates_root: Path
    state_dir: Path
    hosts_file: Path

    def set_dir(self, name: str) -> Path:
        return self.sets_root / name

    def make_repo_dir(self, name: str, *, opt_in: bool = True) -> Path:
        path = self.cwd / name
        path.mkdir(parents=True)
        if opt_in:
            (path / ".sandbox").mkdir()
        return path


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
        # The hosts block is written straight to this file; never via sudo in tests.
        hosts_file=root / "hosts",
    )
    environment.config_file.write_text(
        f"sets_root: {environment.sets_root}\n"
        f"templates_root: {environment.templates_root}\n"
        f"state_dir: {environment.state_dir}\n"
        f"hosts_file: {environment.hosts_file}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("SANDBOX_CONFIG", str(environment.config_file))
    monkeypatch.chdir(cwd)
    return environment


def invoke(
    args: Sequence[str], git: Git, compose: Compose | None = None, docker: Docker | None = None
) -> Result:
    """Run the CLI in-process. Compose defaults to a fake so git-only tests need no Docker;
    Docker queries default to a fake that reports what that Compose fake is running."""
    compose = compose if compose is not None else FakeCompose()
    if docker is None:
        docker = FakeDocker(compose) if isinstance(compose, FakeCompose) else SubprocessDocker()
    deps = Deps(git=git, compose=compose, docker=docker)
    return CliRunner().invoke(app, list(args), obj=deps)
