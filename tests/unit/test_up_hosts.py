"""`up` writes the set's `# sandbox:<set>` hosts block; `down` and rollback remove it."""

from pathlib import Path

import pytest

from sandbox.errors import SandboxError
from tests.conftest import Env, invoke
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose

MYSQL_AND_REDIS = """\
services:
  mysql:
    image: mysql:8.4
  redis:
    image: redis:7
"""

OTHER_ENTRIES = "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost\n10.0.0.5 nas.home  # mine\n"


def make_repo(env: Env, git: FakeGit, name: str, compose: str) -> Path:
    path = env.make_repo_dir(name)
    git.add_repo(path)
    (path / ".sandbox" / "compose.yaml").write_text(compose)
    return path


def test_up_writes_a_block_with_exactly_the_sets_hostnames(env: Env) -> None:
    env.hosts_file.write_text(OTHER_ENTRIES)
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    result = invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose())

    assert result.exit_code == 0, result.output
    assert env.hosts_file.read_text() == (
        OTHER_ENTRIES + "# sandbox:jira-123\n"
        "127.42.0.1\tmysql.jira-123.test\n"
        "127.42.0.2\tredis.jira-123.test\n"
        "# end sandbox:jira-123\n"
    )


def test_two_sets_get_separate_blocks_and_a_rerun_does_not_duplicate(env: Env) -> None:
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    for set_name in ("jira-123", "jira-456", "jira-123"):
        result = invoke([set_name, "up", "--repos", "api"], git, FakeCompose())
        assert result.exit_code == 0, result.output

    text = env.hosts_file.read_text()
    assert text.count("# sandbox:jira-123\n") == 1
    assert "127.43.0.1" not in text  # leases come from one /16; second set gets 127.42.1.x
    assert "127.42.1.1\tmysql.jira-456.test\n" in text


def test_a_failed_compose_up_writes_no_block(env: Env) -> None:
    """The block is written only once the services are up."""
    env.hosts_file.write_text(OTHER_ENTRIES)
    git = FakeGit()
    make_repo(env, git, "api", MYSQL_AND_REDIS)
    compose = FakeCompose(up_error="container sandbox-jira-123-mysql-1 is unhealthy")

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 1
    assert env.hosts_file.read_text() == OTHER_ENTRIES


def test_down_removes_only_the_sets_block(env: Env) -> None:
    env.hosts_file.write_text(OTHER_ENTRIES)
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL_AND_REDIS)
    assert invoke(["jira-123", "up", "--repos", "api"], git, compose).exit_code == 0
    assert invoke(["jira-456", "up", "--repos", "api"], git, compose).exit_code == 0

    result = invoke(["jira-123", "down"], git, compose)

    assert result.exit_code == 0, result.output
    text = env.hosts_file.read_text()
    assert text.startswith(OTHER_ENTRIES)
    assert "jira-123" not in text
    assert "# sandbox:jira-456\n" in text


def test_a_set_without_services_writes_no_block(env: Env) -> None:
    env.hosts_file.write_text(OTHER_ENTRIES)
    git = FakeGit()
    make_repo(env, git, "api", "services: {}\n")

    assert invoke(["jira-123", "up", "--repos", "api"], git, FakeCompose()).exit_code == 0

    assert env.hosts_file.read_text() == OTHER_ENTRIES


def test_rollback_removes_the_block(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from sandbox import provisioner

    async def broken(ctx: provisioner.UpContext) -> None:
        assert "# sandbox:jira-123\n" in env.hosts_file.read_text()  # written before this
        raise SandboxError("init hook failed")

    monkeypatch.setattr(
        provisioner,
        "PROVISION_STEPS",
        [*provisioner.PROVISION_STEPS, provisioner.Step("x", broken)],
    )
    env.hosts_file.write_text(OTHER_ENTRIES)
    git, compose = FakeGit(), FakeCompose()
    make_repo(env, git, "api", MYSQL_AND_REDIS)

    result = invoke(["jira-123", "up", "--repos", "api"], git, compose)

    assert result.exit_code == 1
    assert "init hook failed" in result.stderr
    assert env.hosts_file.read_text() == OTHER_ENTRIES
