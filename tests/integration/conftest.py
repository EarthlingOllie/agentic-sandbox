"""Integration fixtures: real git repositories in a temp directory.

Docker resources are global to the host, so every set name a test hands to real Docker
Compose goes through :func:`unique_set`. Compose derives the project, container, network and
volume names from it, so test runs from several worktrees (or an aborted earlier run's
leftovers) never share a project. Set ``SANDBOX_TEST_RUN_ID`` to pin the prefix.

Published ports are global to the host too: every service binds ``<leased IP>:<native
port>``. So each run claims a loopback /16 of its own (:func:`run_ip_pool`) and every test
gets a /20 of it as its ``ip_pool`` (:func:`ip_pool`), never the default 127.42.0.0/16 that
real sets lease from. See :mod:`tests.ip_pools` for the claim. Set ``SANDBOX_TEST_IP_POOL``
to pin the run's pool instead.
"""

import itertools
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable, Iterator
from ipaddress import IPv4Network
from pathlib import Path

import pytest

from sandbox.names import validate_set_name
from tests.conftest import Env
from tests.ip_pools import claim_run_pool

if shutil.which("git") is None:
    pytest.skip("git is not installed", allow_module_level=True)

RUN_ID = os.environ.get("SANDBOX_TEST_RUN_ID") or secrets.token_hex(4)

TEST_POOL_PREFIX = 20  # 16 /24 blocks per test; a test's sets lease from its own /20


@pytest.fixture(scope="session")
def run_ip_pool() -> Iterator[IPv4Network]:
    """This run's own loopback /16, held for the whole session."""
    pinned = os.environ.get("SANDBOX_TEST_IP_POOL")
    if pinned:
        yield IPv4Network(pinned)
        return
    pool, handle = claim_run_pool()
    try:
        yield pool
    finally:
        handle.close()


_test_pools = itertools.count()


@pytest.fixture(autouse=True)
def ip_pool(env: Env, run_ip_pool: IPv4Network) -> IPv4Network:
    """The pool this test's sets lease from: the next /20 of the run's /16, in its config."""
    blocks = list(run_ip_pool.subnets(new_prefix=TEST_POOL_PREFIX))
    pool = blocks[next(_test_pools) % len(blocks)]
    with env.config_file.open("a") as config:
        config.write(f"ip_pool: {pool}\n")
    return pool


def ip_in_block(pool: IPv4Network, block: int, host: int) -> str:
    """The address ``host`` of the pool's ``block``-th /24 (what the lease hands out)."""
    return str(pool.network_address + block * 256 + host)


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
