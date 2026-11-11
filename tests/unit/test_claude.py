"""`sandbox SET claude`: Claude Code in the agent via `docker compose exec`."""

import stat
from dataclasses import dataclass
from pathlib import Path

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose


@dataclass
class LoginCompose(FakeCompose):
    """Claude Code's first-run `/login`: the exec writes credentials into the shared mount
    (``~/.claude`` on the host, ``CLAUDE_CONFIG_DIR`` in the agent), as Claude Code does."""

    credentials_mode: int = 0o644

    async def exec(
        self, project: str, service: str, command: list[str], workdir: str | None
    ) -> int:
        credentials = claude_dir() / ".credentials.json"
        if not credentials.exists():
            credentials.write_text('{"claudeAiOauth": {"accessToken": "fake"}}')
            credentials.chmod(self.credentials_mode)
        return await super().exec(project, service, command, workdir)


def claude_dir() -> Path:
    return Path.home() / ".claude"


def provision(
    env: Env, set_name: str = "jira-123", compose: FakeCompose | None = None
) -> tuple[FakeGit, FakeCompose]:
    git, compose = FakeGit(), compose if compose is not None else FakeCompose()
    path = env.cwd / "api"
    if not path.exists():
        path = env.make_repo_dir("api")
    git.add_repo(path)
    assert invoke([set_name, "up", "--repos", "api"], git, compose).exit_code == 0
    return git, compose


def test_claude_runs_claude_code_in_the_agent_at_the_set_workspace(env: Env) -> None:
    git, compose = provision(env)

    result = invoke(["jira-123", "claude"], git, compose)

    assert result.exit_code == 0, result.output
    assert compose.execs == [("sandbox-jira-123", "agent", ["claude"], "/workspace/jira-123")]


def test_claude_fails_clearly_when_the_set_is_not_running(env: Env) -> None:
    git, compose = provision(env)
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0

    result = invoke(["jira-123", "claude"], git, compose)

    assert result.exit_code == 1
    assert "set 'jira-123' is not running" in result.stderr
    assert "sandbox jira-123 up" in result.stderr
    assert compose.execs == []


def test_claude_fails_when_only_the_agent_has_stopped(env: Env) -> None:
    git, compose = provision(env)
    compose.stopped_services["sandbox-jira-123"] = {"agent"}

    result = invoke(["jira-123", "claude"], git, compose)

    assert result.exit_code == 1
    assert "not running" in result.stderr
    assert compose.execs == []


def test_claude_on_unknown_set_fails(env: Env) -> None:
    result = invoke(["nope", "claude"], FakeGit(), FakeCompose())

    assert result.exit_code == 1
    assert "no set 'nope'" in result.stderr


def test_first_run_without_credentials_leads_the_user_through_login(env: Env) -> None:
    git, compose = provision(env)

    result = invoke(["jira-123", "claude"], git, compose)

    assert result.exit_code == 0, result.output
    assert "/login" in result.stderr
    assert str(claude_dir()) in result.stderr
    assert compose.execs == [("sandbox-jira-123", "agent", ["claude"], "/workspace/jira-123")]


def test_login_writes_owner_only_credentials_and_a_second_set_starts_logged_in(
    env: Env,
) -> None:
    git, compose = provision(env, "jira-123", LoginCompose())
    provision(env, "jira-456", compose)

    first = invoke(["jira-123", "claude"], git, compose)
    credentials = claude_dir() / ".credentials.json"
    assert first.exit_code == 0, first.output
    assert "/login" in first.stderr
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600

    second = invoke(["jira-456", "claude"], git, compose)

    assert second.exit_code == 0, second.output
    assert "/login" not in second.stderr
    assert compose.execs[-1] == ("sandbox-jira-456", "agent", ["claude"], "/workspace/jira-456")
    # Both agents mount the same host directory as their Claude config.
    for set_name in ("jira-123", "jira-456"):
        override = (env.set_dir(set_name) / "override.yaml").read_text()
        assert f"source: {claude_dir()}" in override


def test_claude_exits_with_claude_codes_exit_status(env: Env) -> None:
    git, compose = provision(env)
    compose.exec_status = 4

    assert invoke(["jira-123", "claude"], git, compose).exit_code == 4
