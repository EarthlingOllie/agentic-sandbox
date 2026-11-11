"""Service deduplication across a set's repos, tested at the resolver's seam.

Repo compose files go in; a merged service graph (or a conflict diff naming both repos)
comes out. The fake Compose stands in for ``docker compose config``; real normalisation is
covered by ``tests/integration/test_dedup.py``.
"""

import asyncio
from pathlib import Path

import pytest

from sandbox.compose.dedup import ServiceConflictError, ServiceGraph, resolve
from sandbox.compose.services import RepoCompose, load_repo_compose
from sandbox.errors import SandboxError
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


def repo(root: Path, name: str, compose: str) -> RepoCompose:
    sandbox_dir = root / name / ".sandbox"
    sandbox_dir.mkdir(parents=True)
    (sandbox_dir / "compose.yaml").write_text(compose)
    loaded = load_repo_compose(name, root / name)
    assert loaded is not None
    return loaded


def run(compose: FakeCompose, *repos: RepoCompose) -> ServiceGraph:
    return asyncio.run(resolve(compose, PROJECT, list(repos)))


def conflict(compose: FakeCompose, *repos: RepoCompose) -> str:
    with pytest.raises(ServiceConflictError) as caught:
        run(compose, *repos)
    return str(caught.value)


def test_identical_services_merge_into_one(tmp_path: Path) -> None:
    api, web = repo(tmp_path, "api", MYSQL), repo(tmp_path, "web", MYSQL)

    graph = run(FakeCompose(), api, web)

    assert graph.names() == ["mysql"]
    assert graph.services["mysql"].repos == ("api", "web")
    assert graph.services["mysql"].definition["image"] == "mysql:8.4"


def test_each_repo_is_normalised_on_its_own(tmp_path: Path) -> None:
    compose = FakeCompose()
    api, web = repo(tmp_path, "api", MYSQL), repo(tmp_path, "web", MYSQL)

    run(compose, api, web)

    assert sorted(compose.configured) == [[api.file], [web.file]]


def test_services_match_by_name_never_by_image(tmp_path: Path) -> None:
    api = repo(tmp_path, "api", MYSQL)
    web = repo(tmp_path, "web", MYSQL.replace("mysql:\n", "db:\n"))

    graph = run(FakeCompose(), api, web)

    assert graph.names() == ["db", "mysql"]
    assert graph.services["db"].repos == ("web",)
    assert graph.services["mysql"].repos == ("api",)


def test_depends_on_labels_ports_and_x_sandbox_are_ignored(tmp_path: Path) -> None:
    api = repo(tmp_path, "api", MYSQL)
    web = repo(
        tmp_path,
        "web",
        MYSQL
        + "    depends_on: [cache]\n"
        + "    labels: {team: web}\n"
        + "    ports: ['3306:3306']\n"
        + "    x-sandbox: {web: false}\n"
        + "  cache:\n    image: redis:7\n",
    )

    graph = run(FakeCompose(), api, web)

    assert graph.services["mysql"].repos == ("api", "web")


HEALTHCHECK = "    healthcheck:\n      test: [CMD, mysqladmin, ping]\n      interval: 2s\n"


@pytest.mark.parametrize("which", ["api", "web"])
def test_a_healthcheck_set_by_only_one_repo_is_kept(tmp_path: Path, which: str) -> None:
    api = repo(tmp_path, "api", MYSQL + (HEALTHCHECK if which == "api" else ""))
    web = repo(tmp_path, "web", MYSQL + (HEALTHCHECK if which == "web" else ""))

    graph = run(FakeCompose(), api, web)

    assert graph.services["mysql"].definition["healthcheck"] == {
        "test": ["CMD", "mysqladmin", "ping"],
        "interval": "2s",
    }


def test_two_differing_healthchecks_conflict(tmp_path: Path) -> None:
    api = repo(tmp_path, "api", MYSQL + HEALTHCHECK)
    web = repo(tmp_path, "web", MYSQL + HEALTHCHECK.replace("2s", "10s"))

    message = conflict(FakeCompose(), api, web)

    assert "service 'mysql' is defined differently by api and web" in message
    assert "-  interval: 2s" in message
    assert "+  interval: 10s" in message


def test_two_identical_healthchecks_merge(tmp_path: Path) -> None:
    api = repo(tmp_path, "api", MYSQL + HEALTHCHECK)
    web = repo(tmp_path, "web", MYSQL + HEALTHCHECK)

    assert run(FakeCompose(), api, web).services["mysql"].repos == ("api", "web")


def test_any_other_difference_fails_with_a_diff_naming_both_repos(tmp_path: Path) -> None:
    api = repo(tmp_path, "api", MYSQL)
    web = repo(tmp_path, "web", MYSQL.replace("mysql:8.4", "mysql:8.0"))

    message = conflict(FakeCompose(), api, web)

    assert message.splitlines()[0] == "service 'mysql' is defined differently by api and web:"
    assert "--- api: mysql" in message
    assert "+++ web: mysql" in message
    assert "-image: mysql:8.4" in message
    assert "+image: mysql:8.0" in message


def test_every_conflicting_repo_is_reported(tmp_path: Path) -> None:
    api = repo(tmp_path, "api", MYSQL)
    jobs = repo(tmp_path, "jobs", MYSQL.replace("mysql:8.4", "mysql:8.0"))
    web = repo(tmp_path, "web", MYSQL.replace("sandbox", "secret"))

    message = conflict(FakeCompose(), api, jobs, web)

    assert "defined differently by api and jobs" in message
    assert "defined differently by api and web" in message


def test_a_compose_config_failure_names_the_repo(tmp_path: Path) -> None:
    api = repo(tmp_path, "api", MYSQL)

    with pytest.raises(SandboxError, match=r"^api: .*unknown key"):
        run(FakeCompose(config_error="unknown key"), api)


# Through the CLI: `up` of a multi-repo set.


def make_cli_repo(env: Env, git: FakeGit, name: str, compose: str) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def test_up_starts_one_shared_service_from_every_repo_file(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    api = make_cli_repo(env, git, "api", MYSQL)
    web = make_cli_repo(env, git, "web", MYSQL + HEALTHCHECK)

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git, compose)

    assert result.exit_code == 0, result.output
    override = env.set_dir("jira-123") / "override.yaml"
    assert compose.running == {
        "sandbox-jira-123": [api / ".sandbox/compose.yaml", web / ".sandbox/compose.yaml", override]
    }
    assert "services: mysql healthy" in result.stdout


def test_conflicting_services_fail_up_before_anything_is_created(env: Env) -> None:
    git, compose = FakeGit(), FakeCompose()
    make_cli_repo(env, git, "api", MYSQL)
    make_cli_repo(env, git, "web", MYSQL.replace("mysql:8.4", "mysql:8.0"))

    result = invoke(["jira-123", "up", "--repos", "api", "web"], git, compose)

    assert result.exit_code == 1
    assert "service 'mysql' is defined differently by api and web" in result.stderr
    assert "+image: mysql:8.0" in result.stderr
    assert ("up", "sandbox-jira-123") not in compose.calls
    assert not env.sets_root.exists()


def test_services_only_a_later_repo_declares_are_restated_in_the_override(env: Env) -> None:
    git = FakeGit()
    make_cli_repo(env, git, "api", MYSQL)
    make_cli_repo(
        env,
        git,
        "web",
        "services:\n  cache:\n    image: redis:7\n    volumes: ['/abs/redis.conf:/redis.conf']\n"
        "    labels: {team: web}\n",
    )

    assert invoke(["jira-123", "up", "--repos", "api", "web"], git, FakeCompose()).exit_code == 0

    text = (env.set_dir("jira-123") / "override.yaml").read_text()
    cache = load_compose_yaml(env.set_dir("jira-123") / "override.yaml")["services"]["cache"]
    # Compose resolves every file's relative paths from the first file's directory, so the
    # normalised (absolute) definition replaces web's own with `!override`.
    assert "volumes: !override" in text
    assert "env_file: !reset" in text
    assert cache["image"] == "redis:7"
    assert cache["volumes"] == ["/abs/redis.conf:/redis.conf"]
    assert cache["labels"] == {"sandbox.set": "jira-123"}  # labels stay with their fragment
