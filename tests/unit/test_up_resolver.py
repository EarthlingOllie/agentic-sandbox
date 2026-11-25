"""`up` starts the shared resolver and attaches it to the set; `down`, a rolled-back `up`,
`prune` and `destroy` detach it and leave it running for other sets (fake Docker)."""

import asyncio
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from sandbox.errors import SandboxError
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose
from tests.fakes_docker import FakeDocker

MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n"


def make_repo(env: Env, git: FakeGit, name: str, compose: str = MYSQL) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def test_up_starts_the_resolver_and_attaches_it_to_the_set(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api")

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose, docker)

    assert result.exit_code == 0, result.output
    assert docker.resolver_domain == "test"
    assert docker.resolver_networks == {"sandbox-jira-123_default"}


def test_the_resolver_forwards_the_configured_domain(env: Env) -> None:
    with env.config_file.open("a") as config:
        config.write("domain: sandbox.example\n")
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api")

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose, docker)

    assert result.exit_code == 0, result.output
    assert docker.resolver_domain == "sandbox.example"


def test_two_sets_share_one_resolver(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api")

    for set_name in ("jira-123", "jira-456", "jira-123"):
        result = invoke([set_name, "up", "--repos", "api"], git, compose, docker)
        assert result.exit_code == 0, result.output

    assert docker.resolver_starts == 1
    assert docker.resolver_networks == {"sandbox-jira-123_default", "sandbox-jira-456_default"}


def test_a_set_without_services_does_not_start_the_resolver(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api", "services: {}\n")

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose, docker)

    assert result.exit_code == 0, result.output
    assert docker.resolver_domain is None
    assert docker.resolver_networks == set()


def test_up_of_a_running_set_brings_a_missing_resolver_back(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api")
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0
    # The resolver was removed behind sandbox's back (its attachments went with it).
    compose.resolver_networks.clear()
    docker = FakeDocker(compose)

    result = invoke(["jira-123", "up"], git, compose, docker)

    assert result.exit_code == 0, result.output
    assert "already running" in result.stdout
    assert docker.resolver_domain == "test"
    assert docker.resolver_networks == {"sandbox-jira-123_default"}


def test_down_detaches_the_resolver_from_that_set_only(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api")
    for set_name in ("jira-123", "jira-456"):
        assert invoke([set_name, "up", "--repos", "api"], git, compose, docker).exit_code == 0

    result = invoke(["jira-123", "down"], git, compose, docker)

    assert result.exit_code == 0, result.output
    assert "sandbox-jira-123" not in compose.running  # its network could be removed
    assert docker.resolver_networks == {"sandbox-jira-456_default"}
    assert docker.resolver_domain == "test"  # still running, for the other set


def test_a_rolled_back_up_detaches_the_resolver(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from sandbox import provisioner

    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)

    async def broken(ctx: provisioner.UpContext) -> None:
        assert docker.resolver_networks == {"sandbox-jira-123_default"}  # attached before this
        raise SandboxError("a later step failed")

    monkeypatch.setattr(
        provisioner,
        "PROVISION_STEPS",
        [*provisioner.PROVISION_STEPS, provisioner.Step("x", broken)],
    )
    make_repo(env, git, "api")

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose, docker)

    assert result.exit_code == 1
    assert "a later step failed" in result.stderr
    assert "rolled back set 'jira-123'" in result.stderr
    assert docker.resolver_networks == set()
    assert compose.running == {}
    assert docker.resolver_domain == "test"  # the resolver itself is left running


def test_a_resolver_that_cannot_start_fails_up_and_rolls_the_set_back(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose, resolver_error="port is already allocated")
    make_repo(env, git, "api")

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose, docker)

    assert result.exit_code == 1
    assert "port is already allocated" in result.stderr
    assert compose.running == {}
    assert docker.resolver_networks == set()


def test_a_failed_resume_of_a_set_the_resolver_is_on_rolls_back_cleanly(env: Env) -> None:
    """The set ran before, so the resolver is on its network when the failed `up` removes it."""
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api")
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose, docker).exit_code == 0
    compose.stopped_services["sandbox-jira-123"] = {"mysql"}
    compose.up_error = "container sandbox-jira-123-mysql-1 is unhealthy"

    result = invoke(["jira-123", "up"], git, compose, docker)

    assert result.exit_code == 1
    assert "rolled back set 'jira-123'" in result.stderr
    assert compose.running == {}
    assert docker.resolver_networks == set()


def test_recreate_moves_the_resolver_to_the_new_network(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api")
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose, docker).exit_code == 0

    result = invoke(["jira-123", "up", "--recreate"], git, compose, docker)

    assert result.exit_code == 0, result.output
    assert "recreated" in result.stdout
    assert docker.resolver_networks == {"sandbox-jira-123_default"}
    assert docker.resolver_starts == 1


def test_prune_detaches_the_resolver_and_leaves_it_for_other_sets(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api")
    for set_name in ("jira-123", "jira-456"):
        assert invoke([set_name, "up", "--repos", "api"], git, compose, docker).exit_code == 0
    # Its containers stopped without `down`: the network, with the resolver on it, is left.
    compose.stopped_services["sandbox-jira-123"] = {"mysql", "agent"}

    result = invoke(["jira-123", "prune"], git, compose, docker)

    assert result.exit_code == 0, result.output
    assert "sandbox-jira-123" not in compose.running
    assert docker.resolver_networks == {"sandbox-jira-456_default"}
    assert docker.resolver_domain == "test"


def test_destroy_detaches_the_resolver_and_leaves_it_for_other_sets(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    docker = FakeDocker(compose)
    make_repo(env, git, "api")
    for set_name in ("jira-123", "jira-456"):
        assert invoke([set_name, "up", "--repos", "api"], git, compose, docker).exit_code == 0
    compose.stopped_services["sandbox-jira-123"] = {"mysql", "agent"}

    result = invoke(["jira-123", "destroy"], git, compose, docker)

    assert result.exit_code == 0, result.output
    assert not env.set_dir("jira-123").exists()
    assert "sandbox-jira-123" not in compose.running
    assert docker.resolver_networks == {"sandbox-jira-456_default"}
    assert docker.resolver_domain == "test"


@dataclass
class MachineDocker(FakeDocker):
    """A Docker whose resolver is state on the machine (a directory), shared by every
    process, and which starts it the way Docker does: look, then, a moment later, create."""

    machine: Path = Path()

    async def ensure_resolver(self, domain: str) -> None:
        running = self.machine / "resolver"
        if not running.exists():
            await asyncio.sleep(0.3)
            running.write_text(domain)
            (self.machine / f"started-by-{os.getpid()}").touch()
        self.resolver_domain = domain

    async def attach_resolver(self, network: str) -> None:
        await super().attach_resolver(network)
        (self.machine / f"attached-{network}").touch()


def _up_at(start_at: float, set_name: str, config: str, cwd: str, machine: str) -> tuple[int, str]:
    """One `up` in a process of its own, started at the same moment as the others."""
    os.environ["SANDBOX_CONFIG"] = config
    os.chdir(cwd)
    git, compose = FakeGit(), FakeCompose()
    git.add_repo(Path(cwd) / "api")
    docker = MachineDocker(compose, machine=Path(machine))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sandbox.platforms.current_platform", lambda: "linux")
        time.sleep(max(0.0, start_at - time.time()))
        result = invoke([set_name, "up", "--repos", "api"], git, compose, docker)
    return result.exit_code, result.output


def test_ups_started_at_the_same_moment_start_one_resolver_attached_to_every_set(
    env: Env, tmp_path: Path
) -> None:
    make_repo(env, FakeGit(), "api")
    machine = tmp_path / "machine"
    machine.mkdir()
    sets = ["jira-1", "jira-2", "jira-3", "jira-4"]
    start_at = time.time() + 2

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=len(sets), mp_context=context) as pool:
        results = list(
            pool.map(
                _up_at,
                [start_at] * len(sets),
                sets,
                [str(env.config_file)] * len(sets),
                [str(env.cwd)] * len(sets),
                [str(machine)] * len(sets),
            )
        )

    assert [code for code, _ in results] == [0] * len(sets), results
    assert len(list(machine.glob("started-by-*"))) == 1
    assert sorted(path.name for path in machine.glob("attached-*")) == [
        f"attached-sandbox-{set_name}_default" for set_name in sets
    ]
