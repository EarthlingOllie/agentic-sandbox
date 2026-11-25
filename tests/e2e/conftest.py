"""The walkthrough harness's machine: the real ``sandbox`` CLI, as a user, on a machine it
really changes.

The harness runs only when ``SANDBOX_E2E=1``. It is meant for a throwaway Linux machine
with systemd-resolved and passwordless sudo (the ``e2e`` GitHub Actions workflow), or a Mac
with OrbStack, because it does what a user does once and nothing else may:
``sudo sandbox setup`` points the machine's lookups under the domain at the resolver and
installs the mkcert CA (into the system trust store). It ends with ``setup --uninstall``;
the mkcert CA is left installed, as ``--uninstall`` itself leaves it.

``SANDBOX_E2E_KEEP_SETUP=1`` (with ``SANDBOX_E2E=1``) is for a machine that is already set
up: the harness runs neither ``setup`` nor ``setup --uninstall``, needs no sudo, uses the
domain that setup installed, and leaves the resolver running.

Nothing runs in-process and nothing is faked: each step is a ``sandbox`` subprocess, run as
the (non-root) invoking user with their real ``HOME`` and the operating system's resolver.
Only the sets root, templates root and state directory go to a temp directory, through a
config named by ``SANDBOX_CONFIG`` (which ``sudo`` is told to keep), so the harness never
reads or writes the user's own sets or templates.
"""

import os
import secrets
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from sandbox.names import DEFAULT_DOMAIN, validate_set_name
from sandbox.platforms import Host
from sandbox.resolver_config import installed_domain

# Opt-in only: the harness changes the machine it runs on (see the module docstring).
E2E_ENABLED = os.environ.get("SANDBOX_E2E") == "1"
SKIP_REASON = (
    "walkthrough harness: changes the machine (sudo setup); set SANDBOX_E2E=1 on a throwaway "
    "machine, or SANDBOX_E2E=1 SANDBOX_E2E_KEEP_SETUP=1 on one that is already set up"
)
# The second switch: leave an existing setup in place (no `setup`, no `setup --uninstall`).
KEEP_SETUP = os.environ.get("SANDBOX_E2E_KEEP_SETUP") == "1"

FIXTURES = Path(__file__).parent.parent / "fixtures"
RUN_ID = os.environ.get("SANDBOX_TEST_RUN_ID") or secrets.token_hex(3)
# Generous: the first `up` may build the agent image and pull mysql.
STEP_TIMEOUT = 1200
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Sandbox E2E",
    "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
    "GIT_COMMITTER_NAME": "Sandbox E2E",
    "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
}
REPORT_KEY: Final = pytest.StashKey[str]()


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Print the walkthrough's report, pass or fail, whatever pytest captured."""
    report = terminalreporter.config.stash.get(REPORT_KEY, None)
    if report is not None:
        terminalreporter.section("sandbox walkthrough report")
        terminalreporter.write(report)


def _require_machine() -> None:
    if sys.platform not in ("linux", "darwin"):
        pytest.fail("the walkthrough harness runs on Linux and on macOS (with OrbStack)")
    if os.geteuid() == 0:
        pytest.fail("run the walkthrough harness as a non-root user (with passwordless sudo)")
    for tool in ("docker", "git", "mkcert", "curl"):
        if shutil.which(tool) is None:
            pytest.fail(f"{tool} is not installed")
    if KEEP_SETUP:
        if installed_domain(Host()) is None:
            pytest.fail(
                "SANDBOX_E2E_KEEP_SETUP=1 keeps an existing setup, but this machine has none: "
                "run `sudo sandbox setup` first, or drop the switch"
            )
    elif subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode:
        pytest.fail(
            "passwordless sudo is required (`sudo -n true` failed); on a machine that is "
            "already set up, SANDBOX_E2E_KEEP_SETUP=1 needs none"
        )


@dataclass(frozen=True)
class Completed:
    # The command as a user would type it (`sandbox SET up`, `sudo sandbox setup`).
    command: str
    status: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


@dataclass
class Machine:
    """The machine, driven the way a user drives it: ``sandbox`` from a shell in ``code``."""

    root: Path
    code: Path
    config: Path
    sets_root: Path
    templates_root: Path
    sandbox: str
    env: dict[str, str]
    # The DNS suffix of set hostnames: the default, or the one an existing setup installed.
    domain: str
    keep_setup: bool
    created_sets: list[str] = field(default_factory=list[str])

    def set_name(self, base: str) -> str:
        name = validate_set_name(f"e2e-{RUN_ID}-{base}")
        self.created_sets.append(name)
        return name

    def run(self, *args: str, sudo: bool = False, stdin: str | None = None) -> Completed:
        """``sandbox ARGS`` as the user, or as ``sudo sandbox ARGS``; ``stdin`` is what the
        user would type into it (nothing, and no terminal, by default)."""
        argv = [self.sandbox, *args]
        shown = shlex.join(["sandbox", *args])
        if sudo:
            # sudo resets PATH (mkcert must be found) and drops SANDBOX_CONFIG: pass both on,
            # as the README tells users to (`sudo env "PATH=$PATH" sandbox setup`).
            keep = [f"{key}={self.env[key]}" for key in ("PATH", "SANDBOX_CONFIG")]
            argv = ["sudo", "-n", "env", *keep, *argv]
            shown = f"sudo {shown}"
        if stdin is not None:
            shown += f" <<< {shlex.quote(stdin.strip())}"
        return self._run(argv, shown, stdin)

    def command(self, argv: Sequence[str]) -> Completed:
        """Any other command as the user (docker, git, curl)."""
        return self._run(list(argv), shlex.join(argv), None)

    def _run(self, argv: list[str], shown: str, stdin: str | None) -> Completed:
        done = subprocess.run(
            argv,
            cwd=self.code,
            env=self.env,
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT,
            check=False,
        )
        return Completed(shown, done.returncode, done.stdout, done.stderr)


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)


def _make_repo(code: Path, name: str, fixture: str | None, env: dict[str, str]) -> Path:
    """A git repo ``name`` from a fixture directory, or one that opts in and declares no
    services (so its set is an agent and nothing else)."""
    path = code / name
    if fixture is not None:
        shutil.copytree(FIXTURES / fixture, path)
    else:
        (path / ".sandbox").mkdir(parents=True)
        (path / ".sandbox" / ".gitkeep").write_text("")
        (path / "README.md").write_text(f"# {name}\n")
    _git(path, env, "init", "--quiet", "--initial-branch=main")
    _git(path, env, "add", "-A")
    _git(path, env, "commit", "--quiet", "-m", "initial")
    return path


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    _require_machine()
    root = tmp_path.resolve()
    code = root / "code"
    code.mkdir()
    config = root / "config.yaml"
    sets_root, templates_root = root / "sets", root / "plans"
    domain = (installed_domain(Host()) if KEEP_SETUP else None) or DEFAULT_DOMAIN
    config.write_text(
        f"sets_root: {sets_root}\ntemplates_root: {templates_root}\n"
        f"state_dir: {root / 'state'}\ndomain: {domain}\n"
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
    # Two repos sharing `mysql` (`jobs` ships the init hook), `redis` and a service without
    # ports that listens all the same, a web app with an extra host, and a repo without
    # services. `api` sorts first, as the only repo using `extends: {file: ...}` must.
    _make_repo(code, "api", "shared-mysql-a", env)
    _make_repo(code, "cache", "cache-repo", env)
    _make_repo(code, "jobs", "shared-mysql-b", env)
    _make_repo(code, "web", "web-repo", env)
    _make_repo(code, "docs", None, env)
    return Machine(root, code, config, sets_root, templates_root, sandbox, env, domain, KEEP_SETUP)
