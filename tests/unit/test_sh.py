"""`sandbox SET sh`: a shell in the agent (or a service) via `docker compose exec`."""

from pathlib import Path

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose

MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n"


def provision(env: Env) -> tuple[FakeGit, FakeCompose]:
    git, compose = FakeGit(), FakeCompose()
    for name, services in (("api", MYSQL), ("web", None)):
        path: Path = env.make_repo_dir(name)
        git.add_repo(path)
        if services:
            (path / ".sandbox" / "compose.yaml").write_text(services)
    assert invoke(["jira-123", "up", "--repos", "api", "web"], git, compose).exit_code == 0
    return git, compose


def test_sh_opens_a_shell_in_the_agent_at_the_set_workspace(env: Env) -> None:
    git, compose = provision(env)

    result = invoke(["jira-123", "sh"], git, compose)

    assert result.exit_code == 0, result.output
    assert compose.execs == [("sandbox-jira-123", "agent", ["sh"], "/workspace/jira-123")]


def test_sh_repo_starts_in_that_repos_worktree(env: Env) -> None:
    git, compose = provision(env)

    result = invoke(["jira-123", "sh", "--repo", "web"], git, compose)

    assert result.exit_code == 0, result.output
    assert compose.execs == [("sandbox-jira-123", "agent", ["sh"], "/workspace/jira-123/web")]


def test_sh_unknown_repo_fails_and_lists_the_sets_repos(env: Env) -> None:
    git, compose = provision(env)

    result = invoke(["jira-123", "sh", "--repo", "mobile"], git, compose)

    assert result.exit_code == 2
    assert "no repo 'mobile' in set 'jira-123'" in result.stderr
    assert "api, web" in result.stderr
    assert compose.execs == []


def test_sh_service_opens_a_shell_in_that_service(env: Env) -> None:
    git, compose = provision(env)

    result = invoke(["jira-123", "sh", "mysql"], git, compose)

    assert result.exit_code == 0, result.output
    assert compose.execs == [("sandbox-jira-123", "mysql", ["sh"], None)]


def test_sh_unknown_service_fails_and_suggests_repo(env: Env) -> None:
    git, compose = provision(env)

    result = invoke(["jira-123", "sh", "web"], git, compose)

    assert result.exit_code == 2
    assert "no service 'web' in set 'jira-123'" in result.stderr
    assert "agent, mysql" in result.stderr
    assert "sandbox jira-123 sh --repo web" in result.stderr
    assert compose.execs == []


def test_sh_rejects_a_service_together_with_repo(env: Env) -> None:
    git, compose = provision(env)

    result = invoke(["jira-123", "sh", "mysql", "--repo", "api"], git, compose)

    assert result.exit_code == 2
    assert "--repo" in result.stderr
    assert compose.execs == []


def test_sh_exits_with_the_shells_exit_status(env: Env) -> None:
    git, compose = provision(env)
    compose.exec_status = 3

    assert invoke(["jira-123", "sh"], git, compose).exit_code == 3


def test_sh_on_unknown_set_fails(env: Env) -> None:
    result = invoke(["nope", "sh"], FakeGit(), FakeCompose())

    assert result.exit_code == 1
    assert "no set 'nope'" in result.stderr
