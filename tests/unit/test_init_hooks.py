"""Init hooks: ``<repo>/.sandbox/init/<service>.sh`` runs inside the service after it is healthy.

Tested through the CLI with fake git and fake Compose: the rendered override shows the
read-only mounts, and the fake records every ``compose exec`` the hooks made. The hook really
creating a database is covered by ``tests/integration/test_init_hooks.py``.
"""

from pathlib import Path
from typing import Any

import yaml

from sandbox.compose.dedup import merge
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose, load_compose_yaml

PROJECT = "sandbox-jira-123"

MYSQL = """\
services:
  mysql:
    image: mysql:8.4
    environment:
      MYSQL_ROOT_PASSWORD: sandbox
"""


def make_repo(
    env: Env, git: FakeGit, name: str, compose: str | None, hooks: dict[str, str] | None = None
) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    if compose is not None:
        (path / ".sandbox" / "compose.yaml").write_text(compose)
    for service, script in (hooks or {}).items():
        init = path / ".sandbox" / "init"
        init.mkdir(exist_ok=True)
        (init / f"{service}.sh").write_text(script)
    return path


def override(env: Env) -> dict[str, Any]:
    return yaml.safe_load((env.set_dir("jira-123") / "override.yaml").read_text())


def mysql_volumes(env: Env) -> list[Any]:
    """The override's mysql volumes, ``!override`` tags read as plain values."""
    document = load_compose_yaml(env.set_dir("jira-123") / "override.yaml")
    return document["services"]["mysql"]["volumes"]


def hook_execs(compose: FakeCompose) -> list[tuple[str, list[str]]]:
    return [(service, command) for _project, service, command in compose.captured]


def test_hook_is_mounted_read_only_and_run_in_its_service_after_up(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    api = make_repo(env, git, "api", MYSQL, {"mysql": "mysql -e 'CREATE DATABASE api'\n"})

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 0, result.output
    mysql = override(env)["services"]["mysql"]
    assert {
        "type": "bind",
        "source": str(api / ".sandbox" / "init"),
        "target": "/sandbox/init/api",
        "read_only": True,
    } in mysql["volumes"]
    assert compose.captured == [(PROJECT, "mysql", ["sh", "/sandbox/init/api/mysql.sh"])]
    # `compose up --wait` returned (services healthy) before the hook ran.
    assert compose.calls == [("up", PROJECT)]


def test_repo_without_hooks_runs_nothing(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL)

    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0

    assert compose.captured == []
    assert "volumes" not in override(env)["services"]["mysql"]


def test_hooks_of_two_repos_on_a_shared_service_run_in_repo_name_order(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    # Named so that creation order and repo-name order differ.
    make_repo(env, git, "web", MYSQL, {"mysql": "echo web\n"})
    make_repo(env, git, "api", MYSQL, {"mysql": "echo api\n"})
    make_repo(env, git, "docs", None, {"mysql": "echo docs\n"})

    result = invoke(["jira-123", "up", "--repos", "web", "api", "docs"], git, compose)

    assert result.exit_code == 0, result.output
    assert hook_execs(compose) == [
        ("mysql", ["sh", "/sandbox/init/api/mysql.sh"]),
        ("mysql", ["sh", "/sandbox/init/docs/mysql.sh"]),
        ("mysql", ["sh", "/sandbox/init/web/mysql.sh"]),
    ]
    targets = [v["target"] for v in mysql_volumes(env)]
    assert sorted(targets) == ["/sandbox/init/api", "/sandbox/init/docs", "/sandbox/init/web"]


def test_hooks_run_again_on_a_resume(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL, {"mysql": "true\n"})
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0
    assert invoke(["jira-123", "down"], git, compose).exit_code == 0

    result = invoke(["jira-123", "up"], git, compose)

    assert result.exit_code == 0, result.output
    assert "resumed" in result.stdout
    assert hook_execs(compose) == [("mysql", ["sh", "/sandbox/init/api/mysql.sh"])] * 2


def test_hooks_run_again_on_recreate(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL, {"mysql": "true\n"})
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0

    result = invoke(["jira-123", "up", "--recreate"], git, compose)

    assert result.exit_code == 0, result.output
    assert len(compose.captured) == 2


def test_failing_hook_fails_the_set_rolls_it_back_and_shows_its_output(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL, {"mysql": "exit 3\n"})
    make_repo(env, git, "web", MYSQL, {"mysql": "true\n"})
    compose.captured_results["/sandbox/init/api/mysql.sh"] = (3, "ERROR 1044: access denied\n")

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git, compose)

    assert result.exit_code == 1
    assert "init hook api/.sandbox/init/mysql.sh failed in service 'mysql' (exit 3)" in (
        result.stderr
    )
    assert "ERROR 1044: access denied" in result.stderr
    assert "rolled back set 'jira-123'" in result.stderr
    assert PROJECT not in compose.running
    assert compose.calls[-1] == ("down", PROJECT)
    # The first failure stops the rest: web's hook never ran.
    assert hook_execs(compose) == [("mysql", ["sh", "/sandbox/init/api/mysql.sh"])]
    # Plan and clones are kept, so a fixed hook can be retried with `up`.
    assert (env.set_dir("jira-123") / "sandbox.yaml").exists()


def test_hook_for_an_unknown_service_fails_before_any_container_starts(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL, {"postgres": "true\n"})

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 1
    assert "api: init hook .sandbox/init/postgres.sh targets service 'postgres'" in result.stderr
    assert "mysql" in result.stderr
    assert compose.calls == []
    assert not env.set_dir("jira-123").exists()


def test_files_that_are_not_shell_hooks_are_ignored(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    api = make_repo(env, git, "api", MYSQL, {"mysql": "true\n"})
    (api / ".sandbox" / "init" / "README.md").write_text("notes\n")
    (api / ".sandbox" / "init" / "seed").mkdir()

    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0

    assert hook_execs(compose) == [("mysql", ["sh", "/sandbox/init/api/mysql.sh"])]


def test_hook_mount_survives_a_relocated_service(env: Env) -> None:
    """A service the first repo lacks is restated with ``!override`` volumes; the hook
    mounts must be added to that list, not replace it."""
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", "services:\n  redis:\n    image: redis:7\n")
    data = MYSQL + "    volumes:\n      - data:/var/lib/mysql\nvolumes:\n  data: {}\n"
    make_repo(env, git, "web", data, {"mysql": "true\n"})

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git, compose)

    assert result.exit_code == 0, result.output
    text = (env.set_dir("jira-123") / "override.yaml").read_text()
    assert "volumes: !override" in text
    volumes = mysql_volumes(env)
    assert volumes[0] == "data:/var/lib/mysql"
    assert volumes[1]["target"] == "/sandbox/init/web"


def test_init_hook_volumes_never_count_as_a_dedup_conflict() -> None:
    def mysql(repo: str) -> dict[str, object]:
        return {
            "image": "mysql:8.4",
            "volumes": [
                {"type": "volume", "source": "data", "target": "/var/lib/mysql"},
                {
                    "type": "bind",
                    "source": f"/code/{repo}/.sandbox/init",
                    "target": f"/sandbox/init/{repo}",
                    "read_only": True,
                },
            ],
        }

    graph = merge([("api", {"mysql": mysql("api")}), ("web", {"mysql": mysql("web")})])

    assert graph.services["mysql"].repos == ("api", "web")


def test_two_repos_with_hooks_on_a_shared_service_up_without_conflict(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL, {"mysql": "true\n"})
    make_repo(env, git, "web", MYSQL, {"mysql": "true\n"})

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git, compose)

    assert result.exit_code == 0, result.output
    assert len(compose.captured) == 2
