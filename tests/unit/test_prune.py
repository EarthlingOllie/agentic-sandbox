"""`sandbox SET prune` and bare `sandbox prune`: clear container infrastructure; never the
agent's clones (fake Compose and Docker)."""

import asyncio
import json
import shutil
from pathlib import Path

from sandbox.compose import project_name
from sandbox.compose.cli import ComposeError
from sandbox.docker import Container
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose
from tests.fakes_docker import FakeDocker

MYSQL = """\
services:
  mysql:
    image: mysql:8.4
"""


def make_repo(env: Env, git: FakeGit, name: str = "api") -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(MYSQL)
    return path


def run(args: list[str], git: FakeGit, docker: FakeDocker) -> tuple[int, str, str]:
    result = invoke(args, git, docker.compose, docker)
    return result.exit_code, result.stdout, result.stderr


def up(set_name: str, git: FakeGit, docker: FakeDocker) -> None:
    code, out, err = run([set_name, "up", "--repos", "api"], git, docker)
    assert code == 0, out + err


def states(git: FakeGit, docker: FakeDocker) -> dict[str, str]:
    code, out, err = run(["status", "--json"], git, docker)
    assert code == 0, out + err
    return {entry["name"]: entry["state"] for entry in json.loads(out)["sets"]}


class SlowCompose(FakeCompose):
    """Counts concurrent `down` calls; fails `down` for the projects in `failing`."""

    def __init__(self, failing: set[str] | None = None) -> None:
        super().__init__()
        self.failing = failing or set()
        self.active = 0
        self.peak = 0

    async def down(self, project: str) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            if project in self.failing:
                raise ComposeError(f"docker compose -p {project} down failed: boom")
            await super().down(project)
        finally:
            self.active -= 1


# --- sandbox SET prune ---------------------------------------------------------------------


def test_prune_clears_a_stopped_sets_infrastructure_and_keeps_clones_and_plan(
    env: Env,
) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    up("jira-123", git, docker)
    assert run(["jira-123", "down"], git, docker)[0] == 0
    docker.compose.calls.clear()

    code, out, err = run(["jira-123", "prune"], git, docker)

    assert code == 0, out + err
    assert docker.compose.calls == [("down", project_name("jira-123"))]
    set_dir = env.set_dir("jira-123")
    assert (set_dir / "sandbox.yaml").is_file()
    assert (set_dir / "api" / ".git").is_dir()
    assert git.clone(set_dir / "api").branch == "agent/jira-123"
    assert "jira-123" in out
    assert "clones kept" in out
    assert "lease" not in out
    assert "hosts" not in out


def test_prune_refuses_a_running_set(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    up("jira-123", git, docker)
    docker.compose.calls.clear()

    code, _, err = run(["jira-123", "prune"], git, docker)

    assert code == 1
    assert "jira-123" in err
    assert "running" in err
    assert "sandbox jira-123 down" in err
    assert docker.compose.calls == []


def test_prune_refuses_a_partially_running_set(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    up("jira-123", git, docker)
    docker.compose.stopped_services[project_name("jira-123")] = {"mysql"}

    code, _, err = run(["jira-123", "prune"], git, docker)

    assert code == 1
    assert "partial" in err


def test_prune_of_an_unknown_set_fails(env: Env) -> None:
    docker = FakeDocker()
    code, _, err = run(["jira-123", "prune"], FakeGit(), docker)
    assert code == 1
    assert "no set 'jira-123'" in err
    assert docker.compose.calls == []


# --- bare sandbox prune --------------------------------------------------------------------


def test_bare_prune_clears_every_orphan_and_leaves_real_sets_alone(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    for name in ("alpha", "bravo", "ghost", "gone"):
        up(name, git, docker)
    assert run(["bravo", "down"], git, docker)[0] == 0
    # ghost: its set directory vanished (containers left behind);
    # gone: down, then its set directory vanished (nothing is left: it is not a set).
    assert run(["gone", "down"], git, docker)[0] == 0
    # (The fake reads a project's files, so hand ghost's container to the leftovers.)
    docker.compose.running.pop(project_name("ghost"))
    docker.extra.append(Container("ghost", "mysql", "running", None))
    for name in ("ghost", "gone"):
        shutil.rmtree(env.set_dir(name))
    # A leftover container under a set name no project knows about.
    docker.extra.append(Container("crashed", "mysql", "exited", None))
    assert states(git, docker) == {
        "alpha": "running",
        "bravo": "stopped",
        "crashed": "orphaned",
        "ghost": "orphaned",
    }
    docker.compose.calls.clear()

    code, out, err = run(["prune"], git, docker)

    assert code == 0, out + err
    assert sorted(docker.compose.calls) == [
        ("down", project_name(name)) for name in ("crashed", "ghost")
    ]
    for name in ("crashed", "ghost"):
        assert name in out
    assert "alpha" not in out
    assert "bravo" not in out


def test_bare_prune_with_no_orphans_says_so(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    up("alpha", git, docker)
    docker.compose.calls.clear()

    code, out, _ = run(["prune"], git, docker)

    assert code == 0
    assert "no orphaned sets" in out
    assert docker.compose.calls == []


def test_bare_prune_runs_sets_concurrently_under_a_bound(env: Env) -> None:
    git = FakeGit()
    compose = SlowCompose()
    docker = FakeDocker(compose)
    docker.extra.extend(Container(f"orphan-{i}", "mysql", "exited", None) for i in range(10))

    code, out, err = run(["prune"], git, docker)

    assert code == 0, out + err
    assert len(compose.calls) == 10
    assert 1 < compose.peak <= 4


def test_one_failing_orphan_does_not_stop_the_rest(env: Env) -> None:
    git = FakeGit()
    compose = SlowCompose()
    docker = FakeDocker(compose)
    docker.extra.extend(
        Container(name, "mysql", "exited", None) for name in ("orphan-1", "orphan-2", "orphan-3")
    )
    compose.failing = {project_name("orphan-2")}

    code, out, err = run(["prune"], git, docker)

    assert code == 1
    assert "orphan-2" in err
    assert "boom" in err
    assert "orphan-1" in out
    assert "orphan-3" in out


def test_bare_prune_never_touches_clones_of_a_planless_set_dir(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    make_repo(env, git)
    up("half", git, docker)
    assert run(["half", "down"], git, docker)[0] == 0
    (env.set_dir("half") / "sandbox.yaml").unlink()

    code, out, err = run(["prune"], git, docker)

    assert code == 0, out + err
    assert "half" in out
    assert (env.set_dir("half") / "api" / ".git").is_dir()
