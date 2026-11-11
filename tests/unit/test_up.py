"""`sandbox SET up --repos ...`: plan, the agent's clones, syncing agent/SET, and safety rules
(fake git)."""

import asyncio
import shutil

import pytest
import yaml

from sandbox.clones import CloneManager
from tests.conftest import Env, invoke
from tests.fakes import FakeGit


def make_git(env: Env, *names: str, current: str = "main") -> FakeGit:
    git = FakeGit()
    for name in names:
        env.make_repo_dir(name)
        git.add_repo(env.cwd / name, current=current)
    return git


def test_up_creates_set_dir_plan_and_clones_and_the_agent_branch(env: Env) -> None:
    git = make_git(env, "api", "web")
    git.repo(env.cwd / "web").current = "release/2.4"
    git.repo(env.cwd / "web").branches.add("release/2.4")

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git)

    assert result.exit_code == 0, result.output
    set_dir = env.set_dir("jira-123")
    plan = yaml.safe_load((set_dir / "sandbox.yaml").read_text())
    assert plan == {
        "repos": {
            "api": {"source": str(env.cwd / "api"), "base": "main"},
            "web": {"source": str(env.cwd / "web"), "base": "release/2.4"},
        }
    }
    for name in ("api", "web"):
        assert (set_dir / name / ".git").is_dir()
        assert git.clone(set_dir / name).branch == "agent/jira-123"
        # Synced into the developer's repo straight away.
        assert "agent/jira-123" in git.repo(env.cwd / name).branches
    assert "agent/jira-123" in result.stdout


def test_plan_pins_absolute_sources_whatever_the_cwd_spelling(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = make_git(env, "api")
    (env.root / "link").symlink_to(env.cwd)
    monkeypatch.chdir(env.root / "link")
    result = invoke(["jira-123", "up", "--repos", "api"], git)
    assert result.exit_code == 0, result.output
    plan = yaml.safe_load((env.set_dir("jira-123") / "sandbox.yaml").read_text())
    assert plan["repos"]["api"]["source"] == str((env.cwd / "api").resolve())


def test_repo_without_sandbox_dir_fails_before_anything_is_created(env: Env) -> None:
    git = make_git(env, "api")
    env.make_repo_dir("web", opt_in=False)
    git.add_repo(env.cwd / "web")

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git)

    assert result.exit_code == 1
    assert "web:" in result.stderr
    assert "has no .sandbox/ directory" in result.stderr
    assert not env.set_dir("jira-123").exists()
    assert git.repo(env.cwd / "api").branches == {"main"}


def test_missing_repo_and_non_git_dir_are_all_reported(env: Env) -> None:
    git = make_git(env, "api")
    env.make_repo_dir("notgit")

    result = invoke(["jira-123", "up", "--repos", "missing", "notgit", "api"], git)

    assert result.exit_code == 1
    assert "missing: no repo directory at" in result.stderr
    assert "notgit:" in result.stderr
    assert "is not the root of a git repository" in result.stderr
    assert not env.sets_root.exists()


def test_detached_head_is_rejected(env: Env) -> None:
    git = make_git(env, "api")
    git.repo(env.cwd / "api").current = None
    result = invoke(["jira-123", "up", "--repos", "api"], git)
    assert result.exit_code == 1
    assert "detached HEAD" in result.stderr
    assert not env.sets_root.exists()


def test_surviving_agent_branch_is_reused_with_a_warning(env: Env) -> None:
    git = make_git(env, "api")
    git.repo(env.cwd / "api").branches.add("agent/jira-123")

    result = invoke(["jira-123", "up", "--repos", "api"], git)

    assert result.exit_code == 0, result.output
    assert "reusing existing branch agent/jira-123" in result.stderr
    assert "base 'main' from the plan was ignored" in result.stderr
    assert (env.set_dir("jira-123") / "api" / ".git").exists()


def test_later_up_after_removing_the_set_reuses_the_branch(env: Env) -> None:
    git = make_git(env, "api")
    assert invoke(["jira-123", "up", "--repos", "api"], git).exit_code == 0
    shutil.rmtree(env.set_dir("jira-123"))

    result = invoke(["jira-123", "up", "--repos", "api"], git)

    assert result.exit_code == 0, result.output
    assert "reusing existing branch agent/jira-123" in result.stderr


def test_up_never_overwrites_an_existing_plan(env: Env) -> None:
    git = make_git(env, "api")
    assert invoke(["jira-123", "up", "--repos", "api"], git).exit_code == 0
    plan_file = env.set_dir("jira-123") / "sandbox.yaml"
    edited = "# my notes\n" + plan_file.read_text()
    plan_file.write_text(edited)

    for args in (["jira-123", "up", "--repos", "api"], ["jira-123", "up"]):
        result = invoke(args, git)
        assert result.exit_code == 0, result.output
        assert plan_file.read_text() == edited
        assert "already present" in result.stdout


def test_up_refuses_repos_that_differ_from_the_plan_on_disk(env: Env) -> None:
    git = make_git(env, "api", "web")
    assert invoke(["jira-123", "up", "--repos", "api"], git).exit_code == 0
    plan_file = env.set_dir("jira-123") / "sandbox.yaml"
    before = plan_file.read_text()

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git)

    assert result.exit_code == 1
    assert "already has a different plan" in result.stderr
    assert plan_file.read_text() == before
    assert not (env.set_dir("jira-123") / "web").exists()


def test_up_without_repos_needs_an_existing_plan(env: Env) -> None:
    result = invoke(["jira-123", "up"], FakeGit())
    assert result.exit_code == 1
    assert "has no plan" in result.stderr
    assert not env.sets_root.exists()


def test_repos_flag_needs_names(env: Env) -> None:
    result = invoke(["jira-123", "up", "--repos"], FakeGit())
    assert result.exit_code == 2
    assert "needs at least one repo name" in result.stderr


def test_missing_base_fails_before_anything_is_created(env: Env) -> None:
    git = make_git(env, "api")
    set_dir = env.set_dir("jira-123")
    set_dir.mkdir(parents=True)
    (set_dir / "sandbox.yaml").write_text(
        f"repos:\n  api:\n    source: {env.cwd / 'api'}\n    base: gone\n"
    )
    result = invoke(["jira-123", "up"], git)
    assert result.exit_code == 1
    assert "base 'gone' does not exist" in result.stderr
    assert not (set_dir / "api").exists()


def test_two_sets_on_one_repo_get_separate_clones(env: Env) -> None:
    git = make_git(env, "api")
    assert invoke(["jira-123", "up", "--repos", "api"], git).exit_code == 0
    assert invoke(["jira-456", "up", "--repos", "api"], git).exit_code == 0
    assert git.clone(env.set_dir("jira-123") / "api").branch == "agent/jira-123"
    assert git.clone(env.set_dir("jira-456") / "api").branch == "agent/jira-456"
    assert {"agent/jira-123", "agent/jira-456"} <= git.repo(env.cwd / "api").branches


def test_up_syncs_the_agents_commits_even_when_the_set_is_already_running(env: Env) -> None:
    git = make_git(env, "api")
    assert invoke(["jira-123", "up", "--repos", "api"], git).exit_code == 0
    git.clone(env.set_dir("jira-123") / "api").commits.append("agent work")

    result = invoke(["jira-123", "up"], git)

    assert result.exit_code == 0, result.output
    assert git.repo(env.cwd / "api").commits["agent/jira-123"] == ["agent work"]


def test_clone_creation_is_serialised_per_source_repo(env: Env) -> None:
    git = FakeGit(add_delay=0.05)
    api = env.make_repo_dir("api")
    web = env.make_repo_dir("web")
    git.add_repo(api)
    git.add_repo(web)
    # Separate managers model separate processes: they share only the lock directory.
    managers = [CloneManager(git, env.state_dir / "locks") for _ in range(4)]

    async def run_all() -> None:
        async with asyncio.TaskGroup() as group:
            for index, manager in enumerate(managers):
                for source in (api, web):
                    group.create_task(
                        manager.ensure(
                            repo=source.name,
                            source=source,
                            path=env.root / f"set{index}" / source.name,
                            branch=f"agent/set{index}",
                            base="main",
                        )
                    )

    asyncio.run(run_all())
    assert git.max_concurrent_adds == {api.resolve(): 1, web.resolve(): 1}
    assert len(git.clones) == 8
