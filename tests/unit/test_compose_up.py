"""`up` starts a repo's backing services as Compose project `sandbox-<set>` (fake Compose)."""

from pathlib import Path

import yaml

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose

MYSQL = """\
services:
  mysql:
    image: mysql:8.4
    volumes:
      - data:/var/lib/mysql
volumes:
  data: {}
"""


def make_repo(env: Env, git: FakeGit, name: str, compose: str | None) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    if compose is not None:
        (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def test_up_starts_the_repo_services_as_the_sets_compose_project(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    api = make_repo(env, git, "api", MYSQL)

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 0, result.output
    override = env.set_dir("jira-123") / "override.yaml"
    # The repo's own file comes first, so its relative paths resolve from the repo.
    assert compose.running == {"sandbox-jira-123": [api / ".sandbox" / "compose.yaml", override]}
    assert "sandbox-jira-123" in result.stdout
    assert "mysql" in result.stdout


def test_override_labels_every_resource_with_the_set_name(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL + "networks:\n  back: {}\n  shared:\n    external: true\n")

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    override = yaml.safe_load((env.set_dir("jira-123") / "override.yaml").read_text())
    label = {"sandbox.set": "jira-123"}
    assert override == {
        "name": "sandbox-jira-123",
        "services": {"mysql": {"labels": label}},
        # External networks belong to someone else, so sandbox leaves them unlabelled.
        "networks": {"back": {"labels": label}, "default": {"labels": label}},
        "volumes": {"data": {"labels": label}},
    }


def test_repo_without_compose_file_still_provisions_its_worktree(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", None)

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 0, result.output
    assert (env.set_dir("jira-123") / "api" / ".git").is_file()
    assert compose.calls == []
    assert not (env.set_dir("jira-123") / "override.yaml").exists()
    assert "services: none" in result.stdout


def test_compose_file_without_services_is_worktree_only(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", "services: {}\n")
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0
    assert compose.calls == []


def test_unhealthy_service_fails_up(env: Env) -> None:
    git = FakeGit()
    compose = FakeCompose(up_error="container sandbox-jira-123-mysql-1 is unhealthy")
    make_repo(env, git, "api", MYSQL)

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 1
    assert "container sandbox-jira-123-mysql-1 is unhealthy" in result.stderr
    assert compose.running == {}


def test_invalid_compose_file_fails_before_anything_is_created(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", "services: [mysql]\n")

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 1
    assert "api:" in result.stderr
    assert "services: expected a mapping" in result.stderr
    assert not env.sets_root.exists()
    assert compose.calls == []
