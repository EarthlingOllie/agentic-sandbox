"""End-to-end fixtures: the real ``sandbox`` CLI, as a user, on a machine it really changes.

The suite runs only when ``SANDBOX_E2E=1``. It is meant for a throwaway Linux runner with
passwordless sudo (the ``e2e`` GitHub Actions workflow), because it does what a user does
once and nothing else may: ``sudo sandbox setup`` installs the hosts helper into
``/usr/local``, a sudoers rule and the mkcert CA (into the system trust store), and every
``up`` writes the real ``/etc/hosts``. It ends with ``setup --uninstall``; the mkcert CA is
left installed, as ``--uninstall`` itself leaves it.

Unlike the unit and integration suites, nothing runs in-process and nothing is faked: each
step is a ``sandbox`` subprocess, run as the (non-root) invoking user with their real
``HOME`` and system resolver. Only the sets root, templates root and state directory go to
a temp directory, through a config named by ``SANDBOX_CONFIG`` (which ``sudo`` is told to
keep), so the suite never reads or writes the user's own sets.
"""

import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.agent import IMAGE_DIR, default_image
from sandbox.hosts_block import HELPER_PATH
from sandbox.names import validate_set_name

# Opt-in only: the suite changes the machine it runs on (see the module docstring).
E2E_ENABLED = os.environ.get("SANDBOX_E2E") == "1"
SKIP_REASON = (
    "end-to-end suite: changes the machine (sudo); set SANDBOX_E2E=1 on a throwaway runner"
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
# The pool the suite's sets lease from; setup refuses if anything else uses it.
IP_POOL = os.environ.get("SANDBOX_E2E_IP_POOL", "127.42.0.0/16")
RUN_ID = os.environ.get("SANDBOX_TEST_RUN_ID") or secrets.token_hex(3)
# Generous: the first `up` may build the agent image and pull mysql.
STEP_TIMEOUT = 1200
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Sandbox E2E",
    "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
    "GIT_COMMITTER_NAME": "Sandbox E2E",
    "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
}


def _require_runner() -> None:
    if sys.platform != "linux":
        pytest.fail("the end-to-end suite runs on Linux (the macOS path is a manual checklist)")
    if os.geteuid() == 0:
        pytest.fail("run the end-to-end suite as a non-root user with passwordless sudo")
    for tool in ("docker", "git", "mkcert", "curl", "getent", "visudo"):
        if shutil.which(tool) is None:
            pytest.fail(f"{tool} is not installed")
    if subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode:
        pytest.fail("passwordless sudo is required (`sudo -n true` failed)")


@dataclass
class Completed:
    argv: list[str]
    status: int
    output: str

    def check(self) -> "Completed":
        assert self.status == 0, f"`{' '.join(self.argv)}` exited {self.status}:\n{self.output}"
        return self


@dataclass
class Running:
    argv: list[str]
    process: subprocess.Popen[str]

    def wait(self) -> Completed:
        output, _ = self.process.communicate(timeout=STEP_TIMEOUT)
        return Completed(self.argv, self.process.returncode, output)


@dataclass
class Machine:
    """The runner, driven the way a user drives it: ``sandbox`` from a shell in ``code``."""

    root: Path
    code: Path
    config: Path
    sets_root: Path
    sandbox: str
    env: dict[str, str]
    created_sets: list[str] = field(default_factory=list[str])

    def set_name(self, base: str) -> str:
        name = validate_set_name(f"e2e-{RUN_ID}-{base}")
        self.created_sets.append(name)
        return name

    def run(self, *args: str, sudo: bool = False) -> Completed:
        """``sandbox ARGS`` as the user, or as ``sudo sandbox ARGS``."""
        return self.start(*args, sudo=sudo).wait()

    def start(self, *args: str, sudo: bool = False) -> "Running":
        """Like :meth:`run`, without waiting for it to finish."""
        argv = [self.sandbox, *args]
        if sudo:
            # sudo resets PATH (mkcert must be found) and drops SANDBOX_CONFIG: pass both on,
            # as the README tells users to (`sudo env "PATH=$PATH" sandbox setup`).
            keep = [f"{key}={self.env[key]}" for key in ("PATH", "SANDBOX_CONFIG")]
            argv = ["sudo", "-n", "env", *keep, *argv]
        process = subprocess.Popen(
            argv,
            cwd=self.code,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return Running(argv, process)

    def command(self, argv: Sequence[str]) -> Completed:
        """Any other command as the user (curl, docker, git, getent)."""
        done = subprocess.run(
            list(argv),
            cwd=self.code,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT,
            check=False,
        )
        return Completed(list(argv), done.returncode, done.stdout + done.stderr)


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)


def _make_repo(code: Path, name: str, fixture: str, env: dict[str, str]) -> Path:
    path = code / name
    shutil.copytree(FIXTURES / fixture, path)
    _git(path, env, "init", "--quiet", "--initial-branch=main")
    _git(path, env, "add", "-A")
    _git(path, env, "commit", "--quiet", "-m", "initial")
    return path


def _cleanup(machine: Machine) -> None:
    """Best effort, so a failed run leaves no containers, hosts blocks or sudo rule behind."""
    for name in machine.created_sets:
        machine.command(
            ["docker", "compose", "-p", project_name(name), "down", "--volumes",
             "--remove-orphans"]
        )  # fmt: skip
        if Path(HELPER_PATH).exists():
            machine.command(["sudo", "-n", str(HELPER_PATH), "remove", name])
    if Path(HELPER_PATH).exists():
        machine.run("setup", "--uninstall", sudo=True)


@pytest.fixture
def machine(tmp_path: Path) -> Iterator[Machine]:
    _require_runner()
    root = tmp_path.resolve()
    code = root / "code"
    code.mkdir()
    config = root / "config.yaml"
    sets_root = root / "sets"
    config.write_text(
        f"ip_pool: {IP_POOL}\n"
        f"sets_root: {sets_root}\n"
        f"templates_root: {root / 'plans'}\n"
        f"state_dir: {root / 'state'}\n"
    )
    env = {
        **os.environ,
        **GIT_IDENTITY,
        "SANDBOX_CONFIG": str(config),
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    sandbox = shutil.which("sandbox", path=str(Path(sys.executable).parent))
    assert sandbox is not None, "the sandbox console script is not installed (uv sync)"
    found = Machine(root, code, config, sets_root, sandbox, env)
    # Two repos sharing `mysql` (`jobs` ships the init hook) and a web app with an extra
    # host. `api` sorts first, as the only repo using `extends: {file: ...}` must.
    _make_repo(code, "api", "shared-mysql-a", env)
    _make_repo(code, "jobs", "shared-mysql-b", env)
    _make_repo(code, "web", "web-repo", env)
    # Build the default agent image once, so the parallel `up`s don't race to build it.
    found.command(["docker", "build", "-q", "-t", default_image(), str(IMAGE_DIR)]).check()
    try:
        yield found
    finally:
        _cleanup(found)
