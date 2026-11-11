"""Integration fixtures: real git repositories in a temp directory.

Docker resources are global to the host, so every set name a test hands to real Docker
Compose goes through :func:`unique_set`. Compose derives the project, container, network and
volume names from it, so test runs from several worktrees (or an aborted earlier run's
leftovers) never share a project. Set ``SANDBOX_TEST_RUN_ID`` to pin the prefix.
"""

import os
import secrets
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from sandbox.names import validate_set_name
from tests.conftest import Env

if shutil.which("git") is None:
    pytest.skip("git is not installed", allow_module_level=True)

RUN_ID = os.environ.get("SANDBOX_TEST_RUN_ID") or secrets.token_hex(4)


def unique_set(base: str) -> str:
    """A set name (hence Compose project ``sandbox-<name>``) unique to this test run."""
    return validate_set_name(f"it-{RUN_ID}-{base}")


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Sandbox Tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "Sandbox Tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}


@pytest.fixture(autouse=True)
def _git_identity(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    # Isolate from the developer's global git config (hooks, signing, default branch).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(env.root / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def make_repo(env: Env) -> Callable[[str], Path]:
    def _make(name: str) -> Path:
        path = env.make_repo_dir(name)
        (path / ".sandbox" / "compose.yaml").write_text("services: {}\n")
        git(path, "init", "--quiet", "--initial-branch=main")
        git(path, "add", "-A")
        git(path, "commit", "--quiet", "-m", "initial")
        return path

    return _make
