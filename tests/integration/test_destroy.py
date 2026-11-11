"""`destroy` against real git: every refusal, the forced paths, clean worktree metadata in
the source repos, and (with real Docker) the root-owned-file fallback."""

import asyncio
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from sandbox.docker import CLEANUP_IMAGE, SubprocessDocker
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.fakes_compose import FakeCompose
from tests.fakes_docker import FakeDocker
from tests.integration.conftest import git, unique_set

pytestmark = pytest.mark.integration

SET = "jira-123"
BRANCH = f"agent/{SET}"

MakeRepo = Callable[[str], Path]


def run(*args: str, docker: SubprocessDocker | FakeDocker | None = None) -> tuple[int, str, str]:
    compose = FakeCompose()
    result = invoke(list(args), SubprocessGit(), compose, docker or FakeDocker(compose))
    return result.exit_code, result.stdout, result.stderr


def make_set(env: Env, make_repo: MakeRepo, *names: str, set_name: str = SET) -> list[Path]:
    repos = [make_repo(name) for name in names]
    code, out, err = run(set_name, "up", "--repos", *names)
    assert code == 0, out + err
    return repos


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").splitlines()


def worktree_paths(repo: Path) -> list[str]:
    listing = git(repo, "worktree", "list", "--porcelain").splitlines()
    return [line.removeprefix("worktree ") for line in listing if line.startswith("worktree ")]


def commit(worktree: Path, name: str, message: str) -> None:
    (worktree / name).write_text(message + "\n")
    git(worktree, "add", name)
    git(worktree, "commit", "--quiet", "-m", message)


def assert_destroyed(env: Env, repos: list[Path], set_name: str = SET) -> None:
    assert not env.set_dir(set_name).exists()
    for repo in repos:
        # Removed through git: no worktree entry and no leftover metadata in the source repo.
        assert worktree_paths(repo) == [str(repo)]
        assert not (repo / ".git" / "worktrees").exists() or not any(
            (repo / ".git" / "worktrees").iterdir()
        )


def assert_intact(env: Env, repos: list[Path]) -> None:
    assert (env.set_dir(SET) / "sandbox.yaml").is_file()
    for repo in repos:
        assert str(env.set_dir(SET) / repo.name) in worktree_paths(repo)
        assert BRANCH in branches(repo)


def test_destroy_removes_worktrees_through_git_and_keeps_branches(
    env: Env, make_repo: MakeRepo
) -> None:
    repos = make_set(env, make_repo, "api", "web")
    commit(env.set_dir(SET) / "api", "feature.txt", "agent work")

    code, out, err = run(SET, "destroy")

    assert code == 0, out + err
    assert_destroyed(env, repos)
    for repo in repos:
        assert BRANCH in branches(repo)
    assert git(repos[0], "log", "-1", "--format=%s", BRANCH) == "agent work"
    assert "kept branch" in out


def test_destroy_refuses_a_running_set(env: Env, make_repo: MakeRepo) -> None:
    repos = [make_repo("api")]
    (repos[0] / ".sandbox" / "compose.yaml").write_text(
        "services:\n  mysql:\n    image: mysql:8.4\n"
    )
    git(repos[0], "commit", "--quiet", "-am", "add mysql")
    compose = FakeCompose()
    docker = FakeDocker(compose)
    up = invoke([SET, "up", "--repos", "api"], SubprocessGit(), compose, docker)
    assert up.exit_code == 0, up.output

    result = invoke([SET, "destroy", "--force"], SubprocessGit(), compose, docker)

    assert result.exit_code == 1
    assert "running" in result.stderr
    assert_intact(env, repos)


def test_destroy_refuses_uncommitted_changes_naming_them(env: Env, make_repo: MakeRepo) -> None:
    repos = make_set(env, make_repo, "api")
    (env.set_dir(SET) / "api" / ".sandbox" / "compose.yaml").write_text("services: {}\n# x\n")

    code, _, err = run(SET, "destroy")

    assert code == 1
    assert "uncommitted changes" in err
    assert ".sandbox/compose.yaml" in err
    assert_intact(env, repos)


def test_destroy_refuses_untracked_files_naming_them(env: Env, make_repo: MakeRepo) -> None:
    repos = make_set(env, make_repo, "api", "web")
    (env.set_dir(SET) / "web" / "notes.txt").write_text("todo\n")

    code, _, err = run(SET, "destroy")

    assert code == 1
    assert "untracked files" in err
    assert "web" in err
    assert "notes.txt" in err
    assert_intact(env, repos)


def test_destroy_force_discards_uncommitted_and_untracked_work(
    env: Env, make_repo: MakeRepo
) -> None:
    repos = make_set(env, make_repo, "api")
    worktree = env.set_dir(SET) / "api"
    (worktree / ".sandbox" / "compose.yaml").write_text("services: {}\n# x\n")
    (worktree / "notes.txt").write_text("todo\n")

    code, out, err = run(SET, "destroy", "--force")

    assert code == 0, out + err
    assert_destroyed(env, repos)


def test_destroy_branches_deletes_agent_branches_without_unpushed_commits(
    env: Env, make_repo: MakeRepo
) -> None:
    repos = make_set(env, make_repo, "api")

    code, out, err = run(SET, "destroy", "--branches")

    assert code == 0, out + err
    assert_destroyed(env, repos)
    assert branches(repos[0]) == ["main"]


def test_destroy_branches_refuses_unpushed_commits(env: Env, make_repo: MakeRepo) -> None:
    repos = make_set(env, make_repo, "api")
    commit(env.set_dir(SET) / "api", "feature.txt", "agent work")

    code, _, err = run(SET, "destroy", "--branches")

    assert code == 1
    assert "unpushed" in err
    assert "agent work" in err
    assert_intact(env, repos)


def test_destroy_branches_deletes_a_branch_whose_commits_are_pushed(
    env: Env, make_repo: MakeRepo
) -> None:
    repos = make_set(env, make_repo, "api")
    worktree = env.set_dir(SET) / "api"
    commit(worktree, "feature.txt", "agent work")
    remote = env.root / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    git(repos[0], "remote", "add", "origin", str(remote))
    git(worktree, "push", "--quiet", "origin", BRANCH)

    code, out, err = run(SET, "destroy", "--branches")

    assert code == 0, out + err
    assert_destroyed(env, repos)
    assert BRANCH not in branches(repos[0])


def test_destroy_branches_force_deletes_unpushed_commits(env: Env, make_repo: MakeRepo) -> None:
    repos = make_set(env, make_repo, "api")
    commit(env.set_dir(SET) / "api", "feature.txt", "agent work")

    code, out, err = run(SET, "destroy", "--branches", "--force")

    assert code == 0, out + err
    assert_destroyed(env, repos)
    assert BRANCH not in branches(repos[0])


docker_only = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


def root_owned_tree(directory: Path) -> None:
    """A root-owned directory with a file in it, written by a container as root."""
    subprocess.run(
        [
            "docker", "run", "--rm", "--pull", "missing", "--network", "none", "--user", "0:0",
            "--volume", f"{directory}:/target", "--entrypoint", "sh", CLEANUP_IMAGE,
            "-c", "mkdir -p /target/cache/sub && echo x > /target/cache/sub/file",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


@docker_only
def test_empty_as_root_deletes_root_owned_files(env: Env) -> None:
    directory = env.root / "scratch"
    directory.mkdir()
    root_owned_tree(directory)
    assert (directory / "cache").stat().st_uid == 0

    asyncio.run(SubprocessDocker().empty_as_root(directory))

    assert directory.is_dir()
    assert list(directory.iterdir()) == []


@docker_only
def test_destroy_deletes_root_owned_files_left_in_a_worktree(env: Env, make_repo: MakeRepo) -> None:
    set_name = unique_set("destroy")
    repo = make_repo("api")
    (repo / ".gitignore").write_text("cache/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "--quiet", "-m", "ignore cache")
    code, out, err = run(set_name, "up", "--repos", "api")
    assert code == 0, out + err
    root_owned_tree(env.set_dir(set_name) / "api")

    code, out, err = run(set_name, "destroy", docker=SubprocessDocker())

    assert code == 0, out + err
    assert_destroyed(env, [repo], set_name)
    if os.geteuid() != 0:
        # Only a non-root developer needs (and gets) the container fallback.
        assert f"deleted root-owned files in {env.set_dir(set_name) / 'api'}" in out
