"""Templates: `sandbox templates`, `save --to`, and `up --from` (fake git)."""

from typing import Any

import pytest
import yaml

from tests.conftest import Env, invoke
from tests.fakes import FakeGit


def make_git(env: Env, *names: str) -> FakeGit:
    git = FakeGit()
    for name in names:
        env.make_repo_dir(name)
        git.add_repo(env.cwd / name)
    return git


def read_template(env: Env, name: str) -> Any:  # noqa: ANN401 - parsed YAML
    return yaml.safe_load((env.templates_root / f"{name}.yaml").read_text())


def test_templates_lists_saved_templates_sorted(env: Env) -> None:
    env.templates_root.mkdir(parents=True)
    for name in ("web-stack", "api-only"):
        (env.templates_root / f"{name}.yaml").write_text("repos:\n  api:\n    source: /code/api\n")
    (env.templates_root / "notes.txt").write_text("not a template\n")

    result = invoke(["templates"], FakeGit())

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["api-only", "web-stack"]


def test_templates_with_no_templates_root_says_so(env: Env) -> None:
    result = invoke(["templates"], FakeGit())
    assert result.exit_code == 0, result.output
    assert f"no templates in {env.templates_root}" in result.stderr
    assert result.stdout == ""


def test_save_writes_a_sets_plan_as_a_template(env: Env) -> None:
    git = make_git(env, "api", "web")
    assert invoke(["jira-123", "up", "--repos", "api", "web"], git).exit_code == 0

    result = invoke(["jira-123", "save", "--to", "stack"], git)

    assert result.exit_code == 0, result.output
    assert read_template(env, "stack") == {
        "repos": {
            "api": {"source": str(env.cwd / "api"), "base": "main"},
            "web": {"source": str(env.cwd / "web"), "base": "main"},
        }
    }
    assert str(env.templates_root / "stack.yaml") in result.stdout
    assert invoke(["templates"], git).stdout.splitlines() == ["stack"]


def test_save_of_an_unknown_set_fails(env: Env) -> None:
    result = invoke(["jira-123", "save", "--to", "stack"], FakeGit())
    assert result.exit_code == 1
    assert "no set 'jira-123'" in result.stderr
    assert not env.templates_root.exists()


def test_save_never_overwrites_an_existing_template(env: Env) -> None:
    git = make_git(env, "api")
    assert invoke(["jira-123", "up", "--repos", "api"], git).exit_code == 0
    env.templates_root.mkdir(parents=True)
    (env.templates_root / "stack.yaml").write_text("# mine\n")

    result = invoke(["jira-123", "save", "--to", "stack"], git)

    assert result.exit_code == 1
    assert "template 'stack' already exists" in result.stderr
    assert (env.templates_root / "stack.yaml").read_text() == "# mine\n"


def test_save_repos_builds_a_template_from_the_current_directory(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = make_git(env, "api", "web")
    (env.root / "link").symlink_to(env.cwd)
    monkeypatch.chdir(env.root / "link")

    result = invoke(["save", "--to", "stack", "--repos", "api", "web"], git)

    assert result.exit_code == 0, result.output
    assert read_template(env, "stack") == {
        "repos": {
            "api": {"source": str(env.cwd / "api")},
            "web": {"source": str(env.cwd / "web")},
        }
    }
    assert not env.sets_root.exists()


def test_save_repos_rejects_a_repo_without_sandbox_dir(env: Env) -> None:
    git = make_git(env, "api")
    env.make_repo_dir("web", opt_in=False)
    git.add_repo(env.cwd / "web")

    result = invoke(["save", "--to", "stack", "--repos", "api", "web"], git)

    assert result.exit_code == 1
    assert "web:" in result.stderr
    assert "has no .sandbox/ directory" in result.stderr
    assert not env.templates_root.exists()


def test_save_needs_exactly_one_of_set_and_repos(env: Env) -> None:
    git = make_git(env, "api")
    neither = invoke(["save", "--to", "stack"], git)
    assert neither.exit_code == 2
    assert "sandbox SET save --to TEMPLATE" in neither.stderr
    both = invoke(["jira-123", "save", "--to", "stack", "--repos", "api"], git)
    assert both.exit_code == 2
    assert "either a SET or --repos" in both.stderr
    assert not env.templates_root.exists()


def write_template(env: Env, name: str, text: str) -> None:
    env.templates_root.mkdir(parents=True, exist_ok=True)
    (env.templates_root / f"{name}.yaml").write_text(text)


def test_up_from_copies_the_template_and_pins_missing_bases(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = make_git(env, "api", "web")
    git.add_repo(env.cwd / "web", current="feature/x")
    api, web = env.cwd / "api", env.cwd / "web"
    write_template(
        env,
        "stack",
        f"repos:\n  api:\n    source: {api}\n    base: release/2.4\n  web:\n    source: {web}\n",
    )
    git.repo(api).branches.add("release/2.4")
    elsewhere = env.root / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = invoke(["jira-123", "up", "--from", "stack"], git)

    assert result.exit_code == 0, result.output
    set_dir = env.set_dir("jira-123")
    assert yaml.safe_load((set_dir / "sandbox.yaml").read_text()) == {
        "repos": {
            "api": {"source": str(api), "base": "release/2.4"},
            "web": {"source": str(web), "base": "feature/x"},
        }
    }
    assert git.clone(set_dir / "api").branch == "agent/jira-123"
    assert git.clone(set_dir / "web").branch == "agent/jira-123"
    # The template itself is left as it was: still reusable.
    assert "base" not in read_template(env, "stack")["repos"]["web"]


def test_up_from_a_template_repo_without_sandbox_dir_fails_before_anything_is_created(
    env: Env,
) -> None:
    git = make_git(env, "api")
    env.make_repo_dir("web", opt_in=False)
    git.add_repo(env.cwd / "web")
    write_template(
        env,
        "stack",
        f"repos:\n  api:\n    source: {env.cwd / 'api'}\n  web:\n    source: {env.cwd / 'web'}\n",
    )

    result = invoke(["jira-123", "up", "--from", "stack"], git)

    assert result.exit_code == 1
    assert "web:" in result.stderr
    assert "has no .sandbox/ directory" in result.stderr
    assert not env.sets_root.exists()
    assert git.repo(env.cwd / "api").branches == {"main"}


def test_up_from_an_unknown_template_fails(env: Env) -> None:
    write_template(env, "other", "repos:\n  api:\n    source: /code/api\n")
    result = invoke(["jira-123", "up", "--from", "stack"], FakeGit())
    assert result.exit_code == 1
    assert "no template 'stack'" in result.stderr
    assert "available: other" in result.stderr
    assert not env.sets_root.exists()


def test_up_from_an_invalid_template_reports_field_errors(env: Env) -> None:
    write_template(env, "stack", "extends: base\nrepos:\n  api:\n    source: code/api\n")
    result = invoke(["jira-123", "up", "--from", "stack"], FakeGit())
    assert result.exit_code == 1
    assert "repos.api.source: Value error, must be an absolute path" in result.stderr
    assert "extends: Extra inputs are not permitted" in result.stderr
    assert not env.sets_root.exists()


def test_from_and_repos_are_mutually_exclusive(env: Env) -> None:
    git = make_git(env, "api")
    write_template(env, "stack", f"repos:\n  api:\n    source: {env.cwd / 'api'}\n")
    result = invoke(["jira-123", "up", "--from", "stack", "--repos", "api"], git)
    assert result.exit_code == 2
    assert "--from and --repos are mutually exclusive" in result.stderr
    assert not env.sets_root.exists()


def test_up_from_on_an_existing_set_keeps_its_plan_or_refuses_a_different_one(
    env: Env,
) -> None:
    git = make_git(env, "api", "web")
    assert invoke(["jira-123", "up", "--repos", "api"], git).exit_code == 0
    plan_file = env.set_dir("jira-123") / "sandbox.yaml"
    before = plan_file.read_text()
    write_template(env, "same", f"repos:\n  api:\n    source: {env.cwd / 'api'}\n")
    write_template(env, "bigger", f"repos:\n  web:\n    source: {env.cwd / 'web'}\n")

    same = invoke(["jira-123", "up", "--from", "same"], git)
    assert same.exit_code == 0, same.output
    bigger = invoke(["jira-123", "up", "--from", "bigger"], git)
    assert bigger.exit_code == 1
    assert "already has a different plan" in bigger.stderr
    assert plan_file.read_text() == before
