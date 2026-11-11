"""`sandbox SET down` stops the set's Compose project and keeps its worktrees (fake Compose)."""

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose


def test_down_stops_the_project_and_keeps_worktrees_and_plan(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    api = env.make_repo_dir("api")
    git.add_repo(api)
    (api / ".sandbox" / "compose.yaml").write_text("services:\n  mysql:\n    image: mysql:8.4\n")
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0

    result = invoke(["jira-123", "down"], git, compose)

    assert result.exit_code == 0, result.output
    assert compose.running == {}
    assert compose.calls[-1] == ("down", "sandbox-jira-123")
    set_dir = env.set_dir("jira-123")
    assert (set_dir / "api" / ".git").is_file()
    assert (set_dir / "sandbox.yaml").is_file()
    assert git.repo(api).checked_out["agent/jira-123"] == set_dir / "api"


def test_down_of_an_unknown_set_fails(env: Env) -> None:
    compose = FakeCompose()
    result = invoke(["jira-123", "down"], FakeGit(), compose)
    assert result.exit_code == 1
    assert "no set 'jira-123'" in result.stderr
    assert compose.calls == []


def test_down_needs_a_set(env: Env) -> None:
    result = invoke(["down"], FakeGit())
    assert result.exit_code == 2
    assert "down needs a set: sandbox SET down" in result.stderr
