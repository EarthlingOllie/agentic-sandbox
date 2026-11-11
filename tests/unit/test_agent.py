"""Every set gets a hardened agent container, checked through the rendered override."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose

MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n"


def make_repo(env: Env, git: FakeGit, name: str, compose: str | None = None) -> Path:
    path = env.make_repo_dir(name)
    (path / ".git").mkdir()
    git.add_repo(path)
    if compose is not None:
        (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def up(env: Env, *repos: str, compose: FakeCompose | None = None) -> dict[str, Any]:
    git = FakeGit()
    for repo in repos:
        make_repo(env, git, repo, MYSQL if repo == "api" else None)
    result = invoke(["jira-123", "up", "--repos", *repos], git, compose or FakeCompose())
    assert result.exit_code == 0, result.output
    override = yaml.safe_load((env.set_dir("jira-123") / "override.yaml").read_text())
    return override["services"]["agent"]


def mounts(agent: dict[str, Any]) -> dict[str, str]:
    """Bind mounts as target -> source; every agent volume is a long-form bind."""
    assert all(volume["type"] == "bind" for volume in agent["volumes"])
    return {volume["target"]: volume["source"] for volume in agent["volumes"]}


def test_every_set_starts_an_agent_even_without_repo_services(env: Env) -> None:
    compose = FakeCompose()
    agent = up(env, "web", compose=compose)

    assert compose.calls == [("up", "sandbox-jira-123")]
    assert agent["labels"] == {"sandbox.set": "jira-123"}
    assert agent["working_dir"] == "/workspace/jira-123"


def test_agent_image_is_built_from_the_bundled_default(env: Env) -> None:
    agent = up(env, "web")

    context = Path(agent["build"]["context"])
    assert (context / "Dockerfile").is_file()
    assert (context / "entrypoint.sh").is_file()
    assert agent["image"].startswith("sandbox-agent:")
    # Built locally when missing; never pulled, so a registry image of that name can't
    # stand in for it.
    assert agent["pull_policy"] == "never"


def test_agent_is_hardened(env: Env) -> None:
    agent = up(env, "api", "web")

    assert agent["cap_drop"] == ["ALL"]
    assert agent["security_opt"] == ["no-new-privileges:true"]
    assert "privileged" not in agent
    uid, gid = agent["user"].split(":")
    assert int(uid) != 0
    assert int(gid) != 0
    assert not any("docker.sock" in source for source in mounts(agent).values())


def test_set_dir_and_each_repos_git_dir_are_mounted_but_not_the_working_tree(
    env: Env,
) -> None:
    agent = up(env, "api", "web")

    api, web = env.cwd / "api", env.cwd / "web"
    assert mounts(agent) == {
        "/workspace/jira-123": str(env.set_dir("jira-123")),
        "/workspace/jira-123/sandbox.yaml": str(env.set_dir("jira-123") / "sandbox.yaml"),
        f"{api}/.git": f"{api}/.git",
        f"{web}/.git": f"{web}/.git",
        "/home/agent/.claude": str(Path.home() / ".claude"),
    }
    # Only the plan is read-only: it pins the agent override (see test_agent_override).
    read_only = [volume["target"] for volume in agent["volumes"] if volume.get("read_only")]
    assert read_only == ["/workspace/jira-123/sandbox.yaml"]


def test_agent_runs_as_the_host_uid_and_gid(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.getuid", lambda: 1234)
    monkeypatch.setattr("os.getgid", lambda: 5678)

    assert up(env, "web")["user"] == "1234:5678"


def test_root_host_user_still_gets_a_non_root_agent(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("os.getuid", lambda: 0)
    monkeypatch.setattr("os.getgid", lambda: 0)

    assert up(env, "web")["user"] == "1000:1000"


def test_host_claude_dir_is_shared_and_claude_json_is_not(env: Env) -> None:
    (Path.home() / ".claude.json").write_text("{}")

    agent = up(env, "web")

    assert agent["environment"]["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"
    assert (Path.home() / ".claude").is_dir()  # created so Docker doesn't make it root-owned
    assert not any(".claude.json" in source for source in mounts(agent).values())


def test_repo_service_named_agent_is_rejected(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", "services:\n  agent:\n    image: busybox\n")

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 1
    assert "api:" in result.stderr
    assert "'agent' is reserved" in result.stderr
    assert compose.calls == []
