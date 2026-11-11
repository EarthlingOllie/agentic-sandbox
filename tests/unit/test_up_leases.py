"""A failed or in-flight first `up` and its IP lease.

The lease is taken during validation (so an exhausted pool fails `up` before anything
starts), which means a brand-new set holds a lease before it has a plan. A failure before
the plan exists must give that lease back, and a bare `prune` running in that window must
not take it away from the `up` that still uses it.
"""

import asyncio
import json
from pathlib import Path

import pytest

from sandbox import provisioner
from sandbox.config import load_config
from sandbox.provisioner import Provisioner
from sandbox.prune import Pruner
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_docker import FakeDocker

MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n"
UP = ["jira-123", "up", "--repos", "api"]


def make_repo(env: Env, git: FakeGit) -> Path:
    path = env.make_repo_dir("api")
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(MYSQL)
    return path


def leased_sets(env: Env) -> dict[str, str]:
    leases = env.state_dir / "leases.json"
    if not leases.exists():
        return {}
    return {name: e["block"] for name, e in json.loads(leases.read_text())["sets"].items()}


def set_states(git: FakeGit, docker: FakeDocker) -> dict[str, str]:
    result = invoke(["status", "--json"], git, docker.compose, docker)
    assert result.exit_code == 0, result.output
    return {entry["name"]: entry["state"] for entry in json.loads(result.stdout)["sets"]}


def test_a_failed_first_up_leaves_no_lease_and_no_orphan(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    api = make_repo(env, git)
    # Fails validation after the lease is taken: no service named postgres.
    (api / ".sandbox" / "init").mkdir()
    (api / ".sandbox" / "init" / "postgres.sh").write_text("echo hi\n")

    result = invoke(UP, git, docker.compose, docker)

    assert result.exit_code == 1
    assert "postgres" in result.stderr
    assert leased_sets(env) == {}
    assert set_states(git, docker) == {}


def test_a_first_up_cancelled_before_its_plan_exists_leaves_no_lease(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted(ctx: provisioner.UpContext) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        provisioner,
        "VALIDATION_STEPS",
        [*provisioner.VALIDATION_STEPS, provisioner.Step("interrupted", interrupted)],
    )
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)

    result = invoke(UP, git, docker.compose, docker)

    assert result.exit_code == 130, result.output
    assert leased_sets(env) == {}


def test_a_failed_up_of_an_existing_set_keeps_its_lease(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    api = make_repo(env, git)
    assert invoke(UP, git, docker.compose, docker).exit_code == 0
    assert invoke(["jira-123", "down"], git, docker.compose, docker).exit_code == 0
    before = leased_sets(env)
    (api / ".sandbox" / "init").mkdir()
    (api / ".sandbox" / "init" / "postgres.sh").write_text("echo hi\n")

    result = invoke(["jira-123", "up"], git, docker.compose, docker)

    assert result.exit_code == 1
    assert leased_sets(env) == before == {"jira-123": "127.42.0.0/24"}


def test_bare_prune_during_an_in_flight_first_up_keeps_its_lease(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    leased, resume = asyncio.Event(), asyncio.Event()

    async def slow_check(ctx: provisioner.UpContext) -> None:
        # A validation step after the lease: the set has a lease and no plan yet.
        if ctx.set_name == "jira-123":
            leased.set()
            await resume.wait()

    steps = list(provisioner.VALIDATION_STEPS)
    at = next(i for i, step in enumerate(steps) if step.name == "ip-leases") + 1
    monkeypatch.setattr(
        provisioner,
        "VALIDATION_STEPS",
        [*steps[:at], provisioner.Step("slow-check", slow_check), *steps[at:]],
    )
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    config = load_config()

    async def scenario() -> None:
        up = asyncio.create_task(
            Provisioner(config, git, docker.compose, lambda _: None).up(
                "jira-123", repos=["api"], cwd=env.cwd
            )
        )
        await leased.wait()
        held = leased_sets(env)
        assert set(held) == {"jira-123"}

        report = await Pruner(config, git, docker.compose, docker).prune_orphans()

        assert report.pruned == []
        assert leased_sets(env) == held
        # Another set's `up` now must not be handed the same block.
        other = await Provisioner(config, git, docker.compose, lambda _: None).up(
            "jira-456", repos=["api"], cwd=env.cwd
        )
        assert other.set_name == "jira-456"
        resume.set()
        await up

    asyncio.run(scenario())

    blocks = leased_sets(env)
    assert set(blocks) == {"jira-123", "jira-456"}
    assert blocks["jira-123"] != blocks["jira-456"]
