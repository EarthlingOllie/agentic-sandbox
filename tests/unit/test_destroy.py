"""`sandbox SET destroy`: sync the agent's branches, prune the set, then remove its clones
and set directory, never silently losing uncommitted, unsynced or unpushed agent work (fake
git, Compose and Docker)."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox.compose import project_name
from sandbox.git import SyncOutcome, SyncResult
from tests.conftest import Env, invoke
from tests.fakes import ROOT_OWNED_MARKER, FakeGit
from tests.fakes_docker import FakeDocker

MYSQL = """\
services:
  mysql:
    image: mysql:8.4
"""

BRANCH = "agent/jira-123"


@pytest.fixture(autouse=True)
def root_owned_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory holding a ``ROOT_OWNED_MARKER`` can't be deleted by the developer, as if a
    container had written root-owned files into it (only the throwaway container can)."""

    def rmtree(path: Path) -> None:
        if path.is_dir() and any(f.name == ROOT_OWNED_MARKER for f in path.rglob("*")):
            raise PermissionError(13, "Permission denied", str(path))
        shutil.rmtree(path)

    monkeypatch.setattr("sandbox.destroy.shutil", SimpleNamespace(rmtree=rmtree))


def make_repo(env: Env, git: FakeGit, name: str) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(MYSQL)
    return path


def run(args: list[str], git: FakeGit, docker: FakeDocker) -> tuple[int, str, str]:
    result = invoke(args, git, docker.compose, docker)
    return result.exit_code, result.stdout, result.stderr


def stopped_set(env: Env, *names: str) -> tuple[FakeGit, FakeDocker, list[Path]]:
    """A set `jira-123` over the given repos, brought up and then down."""
    git, docker = FakeGit(), FakeDocker()
    repos = [make_repo(env, git, name) for name in names]
    code, out, err = run(["jira-123", "up", "--repos", *names], git, docker)
    assert code == 0, out + err
    code, out, err = run(["jira-123", "down"], git, docker)
    assert code == 0, out + err
    docker.compose.calls.clear()
    return git, docker, repos


def leased(env: Env) -> set[str]:
    leases = env.state_dir / "leases.json"
    return set(json.loads(leases.read_text())["sets"]) if leases.exists() else set()


def assert_untouched(env: Env, git: FakeGit, docker: FakeDocker, repos: list[Path]) -> None:
    set_dir = env.set_dir("jira-123")
    assert (set_dir / "sandbox.yaml").is_file()
    for repo in repos:
        assert (set_dir / repo.name / ".git").is_dir()
        assert git.clone(set_dir / repo.name).branch == BRANCH
    assert docker.compose.calls == []
    assert "jira-123" in leased(env)


def test_destroy_prunes_the_set_and_removes_clones_and_set_dir_keeping_branches(
    env: Env,
) -> None:
    git, docker, repos = stopped_set(env, "api", "web")
    (env.set_dir("jira-123") / "certs").mkdir()
    (env.set_dir("jira-123") / "certs" / "leaf.pem").write_text("cert")

    code, out, err = run(["jira-123", "destroy"], git, docker)

    assert code == 0, out + err
    assert docker.compose.calls == [("down", project_name("jira-123"))]
    assert "jira-123" not in leased(env)
    assert not env.set_dir("jira-123").exists()
    for repo in repos:
        assert BRANCH in git.repo(repo).branches
        assert BRANCH not in git.repo(repo).agent_tips
    assert BRANCH in out
    assert "kept" in out


def test_destroy_refuses_a_running_set_even_with_force(env: Env) -> None:
    git, docker = FakeGit(), FakeDocker()
    api = make_repo(env, git, "api")
    assert run(["jira-123", "up", "--repos", "api"], git, docker)[0] == 0
    docker.compose.calls.clear()

    code, _, err = run(["jira-123", "destroy", "--force"], git, docker)

    assert code == 1
    assert "running" in err
    assert "sandbox jira-123 down" in err
    assert (env.set_dir("jira-123") / "api" / ".git").is_dir()
    assert BRANCH in git.repo(api).branches
    assert docker.compose.calls == []


def test_destroy_of_an_unknown_set_fails(env: Env) -> None:
    code, _, err = run(["jira-123", "destroy"], FakeGit(), FakeDocker())
    assert code == 1
    assert "no set 'jira-123'" in err


def test_destroy_refuses_uncommitted_and_untracked_changes_naming_them(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api", "web")
    set_dir = env.set_dir("jira-123")
    api = git.clone(set_dir / "api")
    api.modified, api.changes = True, ("src/app.py",)
    web = git.clone(set_dir / "web")
    web.untracked, web.changes = True, ("notes.txt",)

    code, _, err = run(["jira-123", "destroy"], git, docker)

    assert code == 1
    for named in ("api", "src/app.py", "web", "notes.txt", "--force"):
        assert named in err
    assert_untouched(env, git, docker, repos)


def test_destroy_force_discards_uncommitted_and_untracked_changes(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api")
    clone = git.clone(env.set_dir("jira-123") / "api")
    clone.modified = clone.untracked = True
    clone.changes = ("src/app.py", "notes.txt")

    code, out, err = run(["jira-123", "destroy", "--force"], git, docker)

    assert code == 0, out + err
    assert not env.set_dir("jira-123").exists()
    assert BRANCH in git.repo(repos[0]).branches


def test_destroy_syncs_the_agents_last_commits_before_removing_the_clone(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api")
    git.clone(env.set_dir("jira-123") / "api").commits.append("agent work")

    code, out, err = run(["jira-123", "destroy"], git, docker)

    assert code == 0, out + err
    assert git.repo(repos[0]).commits[BRANCH] == ["agent work"]
    assert not env.set_dir("jira-123").exists()


def test_destroy_refuses_agent_commits_it_could_not_sync(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api", "web")
    git.clone(env.set_dir("jira-123") / "web").sync_result = SyncResult(
        SyncOutcome.DIVERGED, "agent/jira-123 has commits the agent's branch doesn't"
    )

    code, _, err = run(["jira-123", "destroy"], git, docker)

    assert code == 1
    for named in ("web", "diverged", "--force"):
        assert named in err
    assert_untouched(env, git, docker, repos)


def test_destroy_force_discards_agent_commits_it_could_not_sync(env: Env) -> None:
    git, docker, _ = stopped_set(env, "api")
    git.clone(env.set_dir("jira-123") / "api").sync_result = SyncResult(
        SyncOutcome.CHECKED_OUT, "agent/jira-123 is checked out"
    )

    code, out, err = run(["jira-123", "destroy", "--force"], git, docker)

    assert code == 0, out + err
    assert not env.set_dir("jira-123").exists()


def test_destroy_branches_deletes_the_agent_branches(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api", "web")

    code, out, err = run(["jira-123", "destroy", "--branches"], git, docker)

    assert code == 0, out + err
    for repo in repos:
        assert BRANCH not in git.repo(repo).branches
        assert "main" in git.repo(repo).branches
    assert "deleted" in out


def test_destroy_branches_refuses_unpushed_commits_naming_them(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api", "web")
    git.repo(repos[1]).unpushed[BRANCH] = ["abc1234 Add the login page"]

    code, _, err = run(["jira-123", "destroy", "--branches"], git, docker)

    assert code == 1
    for named in ("web", "abc1234 Add the login page", "--force"):
        assert named in err
    assert_untouched(env, git, docker, repos)
    for repo in repos:
        assert BRANCH in git.repo(repo).branches


def test_destroy_keeps_branches_with_unpushed_commits_without_branches_flag(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api")
    git.repo(repos[0]).unpushed[BRANCH] = ["abc1234 Add the login page"]

    code, out, err = run(["jira-123", "destroy"], git, docker)

    assert code == 0, out + err
    assert BRANCH in git.repo(repos[0]).branches


def test_destroy_branches_force_deletes_branches_with_unpushed_commits(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api")
    git.repo(repos[0]).unpushed[BRANCH] = ["abc1234 Add the login page"]

    code, out, err = run(["jira-123", "destroy", "--branches", "--force"], git, docker)

    assert code == 0, out + err
    assert BRANCH not in git.repo(repos[0]).branches
    assert not env.set_dir("jira-123").exists()


def test_destroy_tolerates_a_clone_that_is_already_gone(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api")
    clone = env.set_dir("jira-123") / "api"
    (clone / ".git").rmdir()
    clone.rmdir()

    code, out, err = run(["jira-123", "destroy"], git, docker)

    assert code == 0, out + err
    assert not env.set_dir("jira-123").exists()
    assert BRANCH in git.repo(repos[0]).branches


def test_on_linux_root_owned_files_are_deleted_from_a_throwaway_container(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sandbox.destroy.sys.platform", "linux")
    git, docker, repos = stopped_set(env, "api")
    clone = env.set_dir("jira-123") / "api"
    (clone / "vendor").mkdir()
    (clone / "vendor" / ROOT_OWNED_MARKER).write_text("")

    code, out, err = run(["jira-123", "destroy"], git, docker)

    assert code == 0, out + err
    assert docker.emptied == [clone]
    assert not env.set_dir("jira-123").exists()
    assert BRANCH in git.repo(repos[0]).branches


def test_off_linux_root_owned_files_fail_without_a_container(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sandbox.destroy.sys.platform", "darwin")
    git, docker, _ = stopped_set(env, "api")
    clone = env.set_dir("jira-123") / "api"
    (clone / ROOT_OWNED_MARKER).write_text("")

    code, _, err = run(["jira-123", "destroy"], git, docker)

    assert code == 1
    assert "Permission denied" in err
    assert docker.emptied == []


def test_destroy_needs_a_set(env: Env) -> None:
    code, _, err = run(["destroy"], FakeGit(), FakeDocker())
    assert code == 2
    assert "sandbox SET destroy" in err
