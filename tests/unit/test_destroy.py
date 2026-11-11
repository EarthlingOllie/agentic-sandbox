"""`sandbox SET destroy`: prune the set, then remove its worktrees and set directory, never
silently losing uncommitted or unpushed agent work (fake git, Compose and Docker)."""

import json
from pathlib import Path

import pytest

from sandbox.compose import project_name
from tests.conftest import Env, invoke
from tests.fakes import ROOT_OWNED_MARKER, FakeGit
from tests.fakes_docker import FakeDocker

MYSQL = """\
services:
  mysql:
    image: mysql:8.4
"""

BRANCH = "agent/jira-123"


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
        assert (set_dir / repo.name / ".git").is_file()
        assert git.repo(repo).checked_out[BRANCH] == set_dir / repo.name
    assert docker.compose.calls == []
    assert "jira-123" in leased(env)


def test_destroy_prunes_the_set_and_removes_worktrees_and_set_dir_keeping_branches(
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
        # Removed through git: the source repo no longer has the worktree checked out.
        assert BRANCH not in git.repo(repo).checked_out
        assert BRANCH in git.repo(repo).branches
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
    assert (env.set_dir("jira-123") / "api" / ".git").is_file()
    assert git.repo(api).checked_out[BRANCH] == env.set_dir("jira-123") / "api"
    assert docker.compose.calls == []


def test_destroy_of_an_unknown_set_fails(env: Env) -> None:
    code, _, err = run(["jira-123", "destroy"], FakeGit(), FakeDocker())
    assert code == 1
    assert "no set 'jira-123'" in err


def test_destroy_refuses_uncommitted_and_untracked_changes_naming_them(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api", "web")
    set_dir = env.set_dir("jira-123")
    api = git.worktree(set_dir / "api")
    api.modified, api.changes = True, ("src/app.py",)
    web = git.worktree(set_dir / "web")
    web.untracked, web.changes = True, ("notes.txt",)

    code, _, err = run(["jira-123", "destroy"], git, docker)

    assert code == 1
    for named in ("api", "src/app.py", "web", "notes.txt", "--force"):
        assert named in err
    assert_untouched(env, git, docker, repos)


def test_destroy_force_discards_uncommitted_and_untracked_changes(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api")
    worktree = git.worktree(env.set_dir("jira-123") / "api")
    worktree.modified = worktree.untracked = True
    worktree.changes = ("src/app.py", "notes.txt")

    code, out, err = run(["jira-123", "destroy", "--force"], git, docker)

    assert code == 0, out + err
    assert not env.set_dir("jira-123").exists()
    assert BRANCH not in git.repo(repos[0]).checked_out


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


def test_destroy_tolerates_a_worktree_that_is_already_gone(env: Env) -> None:
    git, docker, repos = stopped_set(env, "api")
    worktree = env.set_dir("jira-123") / "api"
    (worktree / ".git").unlink()
    worktree.rmdir()

    code, out, err = run(["jira-123", "destroy"], git, docker)

    assert code == 0, out + err
    assert not env.set_dir("jira-123").exists()
    assert BRANCH not in git.repo(repos[0]).checked_out


def test_on_linux_root_owned_files_are_deleted_from_a_throwaway_container(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sandbox.destroy.sys.platform", "linux")
    git, docker, repos = stopped_set(env, "api")
    worktree = env.set_dir("jira-123") / "api"
    (worktree / "vendor").mkdir()
    (worktree / "vendor" / ROOT_OWNED_MARKER).write_text("")

    code, out, err = run(["jira-123", "destroy"], git, docker)

    assert code == 0, out + err
    assert docker.emptied == [worktree]
    assert not env.set_dir("jira-123").exists()
    # The source repo's worktree metadata is cleaned up all the same.
    assert BRANCH not in git.repo(repos[0]).checked_out


def test_off_linux_root_owned_files_fail_without_a_container(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sandbox.destroy.sys.platform", "darwin")
    git, docker, _ = stopped_set(env, "api")
    worktree = env.set_dir("jira-123") / "api"
    (worktree / ROOT_OWNED_MARKER).write_text("")

    code, _, err = run(["jira-123", "destroy"], git, docker)

    assert code == 1
    assert "Permission denied" in err
    assert docker.emptied == []


def test_destroy_needs_a_set(env: Env) -> None:
    code, _, err = run(["destroy"], FakeGit(), FakeDocker())
    assert code == 2
    assert "sandbox SET destroy" in err
