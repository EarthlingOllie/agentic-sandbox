"""`up` decides from the set directory what to do, and rolls back what it started on failure.

State table: no set → provision; plan but nothing running → resume; running → no-op;
running with ``--recreate`` → restart. A failed or cancelled `up` takes the set's own
containers, network and volumes down again but keeps the plan and clones.
"""

import asyncio
import signal
from pathlib import Path

import pytest

from sandbox.compose.cli import ComposeError
from sandbox.config import load_config
from sandbox.errors import SandboxError
from sandbox.provisioner import Provisioner
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose

MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n"
UP = ["jira-123", "up", "--repos", "api"]
PROJECT = "sandbox-jira-123"


def make_repo(env: Env, git: FakeGit, name: str = "api", compose: str | None = MYSQL) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    if compose is not None:
        (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


class CountingGit(FakeGit):
    def __init__(self) -> None:
        super().__init__()
        self.adds = 0

    async def clone_for_agent(
        self, repo: Path, path: Path, branch: str, *, base: str | None
    ) -> None:
        self.adds += 1
        await super().clone_for_agent(repo, path, branch, base=base)


def ups(compose: FakeCompose) -> int:
    return sum(1 for verb, _ in compose.calls if verb == "up")


# --- state table ---------------------------------------------------------------------------


def test_no_set_directory_provisions_the_set(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = invoke(UP, git, compose)

    assert result.exit_code == 0, result.output
    assert "provisioned" in result.stdout
    assert PROJECT in compose.running


def test_running_set_is_a_no_op_that_prints_its_services(env: Env) -> None:
    git, compose = CountingGit(), FakeCompose()
    make_repo(env, git)
    assert invoke(UP, git, compose).exit_code == 0
    adds, started = git.adds, ups(compose)

    result = invoke(["jira-123", "up"], git, compose)

    assert result.exit_code == 0, result.output
    assert "already running" in result.stdout
    assert f"services: mysql running [{PROJECT}]" in result.stdout
    assert (git.adds, ups(compose)) == (adds, started)
    assert ("down", PROJECT) not in compose.calls


def test_plan_with_nothing_running_resumes_without_creating_clones(env: Env) -> None:
    git, compose = CountingGit(), FakeCompose()
    make_repo(env, git)
    assert invoke(UP, git, compose).exit_code == 0
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0
    adds = git.adds

    result = invoke(["jira-123", "up"], git, compose)

    assert result.exit_code == 0, result.output
    assert "resumed" in result.stdout
    assert git.adds == adds
    assert PROJECT in compose.running


def test_partially_running_set_is_resumed_not_skipped(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, compose=MYSQL + "  redis:\n    image: redis:7\n")
    assert invoke(UP, git, compose).exit_code == 0
    compose.stopped_services[PROJECT] = {"redis"}

    result = invoke(["jira-123", "up"], git, compose)

    assert result.exit_code == 0, result.output
    assert "resumed" in result.stdout
    assert compose.stopped_services.get(PROJECT, set()) == set()


def test_recreate_restarts_a_running_set(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)
    assert invoke(UP, git, compose).exit_code == 0
    compose.calls.clear()

    result = invoke(["jira-123", "up", "--recreate"], git, compose)

    assert result.exit_code == 0, result.output
    assert "recreated" in result.stdout
    assert compose.calls == [("down", PROJECT), ("up", PROJECT)]
    assert PROJECT in compose.running


def test_recreate_on_a_stopped_set_just_resumes(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)
    assert invoke(UP, git, compose).exit_code == 0
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0
    compose.calls.clear()

    result = invoke(["jira-123", "up", "--recreate"], git, compose)

    assert result.exit_code == 0, result.output
    assert "resumed" in result.stdout
    assert compose.calls == [("up", PROJECT)]


# --- validation before any container starts ----------------------------------------------


def test_registered_validation_runs_before_anything_is_created(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox import provisioner

    async def refuse(ctx: provisioner.UpContext) -> None:
        raise SandboxError(f"hostname clash in set {ctx.set_name}")

    monkeypatch.setattr(
        provisioner,
        "VALIDATION_STEPS",
        [*provisioner.VALIDATION_STEPS, provisioner.Step("clash", refuse)],
    )
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = invoke(UP, git, compose)

    assert result.exit_code == 1
    assert "hostname clash in set jira-123" in result.stderr
    assert not env.sets_root.exists()
    assert compose.calls == []


def test_registered_provision_step_runs_after_services_are_up(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox import provisioner

    seen: list[bool] = []

    async def post_up(ctx: provisioner.UpContext) -> None:
        seen.append(PROJECT in compose.running)

    monkeypatch.setattr(
        provisioner,
        "PROVISION_STEPS",
        [*provisioner.PROVISION_STEPS, provisioner.Step("post-up", post_up)],
    )
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    assert invoke(UP, git, compose).exit_code == 0
    assert seen == [True]


# --- rollback ---------------------------------------------------------------------------


def test_failed_up_rolls_back_services_but_keeps_plan_and_clones(env: Env) -> None:
    git = FakeGit()
    compose = FakeCompose(up_error="container sandbox-jira-123-mysql-1 is unhealthy")
    make_repo(env, git)

    result = invoke(UP, git, compose)

    assert result.exit_code == 1
    assert "is unhealthy" in result.stderr
    assert "rolled back" in result.stderr
    assert compose.calls == [("up", PROJECT), ("down", PROJECT)]
    set_dir = env.set_dir("jira-123")
    assert (set_dir / "sandbox.yaml").is_file()
    assert (set_dir / "api" / ".git").is_dir()

    # Rerunning `up` resumes from what was kept.
    compose.up_error = None
    result = invoke(["jira-123", "up"], git, compose)
    assert result.exit_code == 0, result.output
    assert "resumed" in result.stdout


def test_failing_step_after_compose_up_rolls_back_in_reverse_order(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox import provisioner

    order: list[str] = []

    async def write_hosts(ctx: provisioner.UpContext) -> None:
        order.append("hosts written")

    async def remove_hosts(ctx: provisioner.UpContext) -> None:
        order.append(f"hosts removed, compose running={PROJECT in compose.running}")

    async def broken(ctx: provisioner.UpContext) -> None:
        raise SandboxError("init hook failed")

    monkeypatch.setattr(
        provisioner,
        "PROVISION_STEPS",
        [
            *provisioner.PROVISION_STEPS,
            provisioner.Step("hosts", write_hosts, rollback=remove_hosts),
            provisioner.Step("hooks", broken),
        ],
    )
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)

    result = invoke(UP, git, compose)

    assert result.exit_code == 1
    assert "init hook failed" in result.stderr
    # Later steps are undone first: the hosts block goes while services still run.
    assert order == ["hosts written", "hosts removed, compose running=True"]
    assert compose.running == {}


def test_failure_of_one_set_leaves_other_sets_running(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git)
    assert invoke(["other", "up", "--repos", "api"], git, compose).exit_code == 0

    compose.up_error = "container sandbox-jira-123-mysql-1 is unhealthy"
    assert invoke(UP, git, compose).exit_code == 1

    assert set(compose.running) == {"sandbox-other"}
    assert ("down", "sandbox-other") not in compose.calls


def test_failing_rollback_is_reported_and_the_original_error_kept(env: Env) -> None:
    git = FakeGit()
    compose = FakeCompose(up_error="mysql is unhealthy", down_error="docker daemon went away")
    make_repo(env, git)

    result = invoke(UP, git, compose)

    assert result.exit_code == 1
    assert "mysql is unhealthy" in result.stderr
    assert "docker daemon went away" in result.stderr
    assert "sandbox jira-123 down" in result.stderr


# --- fail fast and cancellation ----------------------------------------------------------


class FailFastGit(FakeGit):
    """`api` fails at once; `web` would take a long time and must be cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.finished: list[str] = []
        self.cancelled: list[str] = []

    async def clone_for_agent(
        self, repo: Path, path: Path, branch: str, *, base: str | None
    ) -> None:
        if repo.name == "api":
            raise ComposeError("api: git clone failed")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled.append(repo.name)
            raise
        self.finished.append(repo.name)


def test_steps_fail_fast_cancelling_sibling_work(env: Env) -> None:
    git, compose = FailFastGit(), FakeCompose()
    make_repo(env, git, "api")
    make_repo(env, git, "web")

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git, compose)

    assert result.exit_code == 1
    assert "api: git clone failed" in result.stderr
    assert git.cancelled == ["web"]
    assert git.finished == []
    assert ("up", PROJECT) not in compose.calls


class HangingCompose(FakeCompose):
    """`up` starts the containers, then blocks waiting for health until cancelled."""

    def __init__(self, *, slow_down: bool = False) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.slow_down = slow_down
        self.down_finished = False

    async def up(self, project: str, files: list[Path]) -> None:
        await super().up(project, files)
        self.started.set()
        await asyncio.Event().wait()

    async def down(self, project: str) -> None:
        if self.slow_down:
            await asyncio.sleep(0.05)
        await super().down(project)
        self.down_finished = True


async def _cancel_mid_up(env: Env, compose: HangingCompose, *, cancels: int) -> None:
    git = FakeGit()
    make_repo(env, git)
    provisioner = Provisioner(load_config(), git, compose, lambda _: None)
    task = asyncio.create_task(provisioner.up("jira-123", repos=["api"], cwd=env.cwd))
    await compose.started.wait()
    for _ in range(cancels):
        task.cancel()
        await asyncio.sleep(0.01)
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancelling_up_rolls_back_the_sets_services(env: Env) -> None:
    compose = HangingCompose()

    asyncio.run(_cancel_mid_up(env, compose, cancels=1))

    assert compose.running == {}
    assert compose.calls[-1] == ("down", PROJECT)
    assert (env.set_dir("jira-123") / "api" / ".git").is_dir()


def test_rollback_is_shielded_from_repeated_cancellation(env: Env) -> None:
    compose = HangingCompose(slow_down=True)

    asyncio.run(_cancel_mid_up(env, compose, cancels=3))

    assert compose.down_finished
    assert compose.running == {}


class CtrlCCompose(FakeCompose):
    """Ctrl-C arrives as a real SIGINT while `up` waits for health, and again while the
    rollback's `down` is running, as when a developer presses Ctrl-C twice."""

    def __init__(self, *, abort_down: bool = False) -> None:
        super().__init__()
        self.abort_down = abort_down
        self.down_finished = False

    async def up(self, project: str, files: list[Path]) -> None:
        await super().up(project, files)
        signal.raise_signal(signal.SIGINT)
        await asyncio.Event().wait()

    async def down(self, project: str) -> None:
        if self.abort_down:
            raise KeyboardInterrupt  # the rollback dies however it dies
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0.05)
        await super().down(project)
        self.down_finished = True


def test_second_ctrl_c_during_rollback_does_not_abort_it(env: Env) -> None:
    git, compose = FakeGit(), CtrlCCompose()
    make_repo(env, git)

    result = invoke(UP, git, compose)

    assert result.exit_code == 130, result.output
    assert compose.down_finished
    assert compose.running == {}
    assert "rolled back set 'jira-123'" in result.stderr
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_aborted_rollback_is_reported_as_incomplete(env: Env) -> None:
    git, compose = FakeGit(), CtrlCCompose(abort_down=True)
    make_repo(env, git)

    result = invoke(UP, git, compose)

    assert result.exit_code == 130, result.output
    assert "rolled back set" not in result.stderr
    assert "rollback of set 'jira-123' was interrupted" in result.stderr
    assert PROJECT in result.stderr
    assert "sandbox jira-123 down" in result.stderr
