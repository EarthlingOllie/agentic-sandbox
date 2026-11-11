"""The privileged `sandbox-hosts` helper rewrites only its set's block and refuses abuse.

The helper is driven through its command line (`run(argv)`), pointed at a temp hosts file.
"""

from pathlib import Path

import pytest

from sandbox.hosts_helper import run

OTHER = "127.0.0.1\tlocalhost\n93.184.215.14 example.com\n"


@pytest.fixture
def hosts(tmp_path: Path) -> Path:
    path = tmp_path / "hosts"
    path.write_text(OTHER)
    return path


def helper(hosts: Path, *argv: str) -> int:
    return run(list(argv), hosts_path=hosts, lock_path=hosts.with_name("hosts.lock"))


def test_write_adds_the_sets_block_and_keeps_other_entries(hosts: Path) -> None:
    status = helper(hosts, "write", "jira-123", "127.42.0.0/24", "mysql.jira-123.test=127.42.0.1")

    assert status == 0
    assert hosts.read_text() == (
        OTHER + "# sandbox:jira-123\n127.42.0.1\tmysql.jira-123.test\n# end sandbox:jira-123\n"
    )


def test_write_replaces_only_the_named_sets_block(hosts: Path) -> None:
    helper(hosts, "write", "a", "127.42.0.0/24", "mysql.a.test=127.42.0.1")
    helper(hosts, "write", "b", "127.42.1.0/24", "mysql.b.test=127.42.1.1")

    assert helper(hosts, "write", "a", "127.42.0.0/24", "redis.a.test=127.42.0.2") == 0

    assert hosts.read_text() == (
        OTHER
        + "# sandbox:a\n127.42.0.2\tredis.a.test\n# end sandbox:a\n"
        + "# sandbox:b\n127.42.1.1\tmysql.b.test\n# end sandbox:b\n"
    )


def test_remove_drops_only_the_named_sets_block(hosts: Path) -> None:
    helper(hosts, "write", "a", "127.42.0.0/24", "mysql.a.test=127.42.0.1")
    helper(hosts, "write", "b", "127.42.1.0/24", "mysql.b.test=127.42.1.1")

    assert helper(hosts, "remove", "a") == 0
    assert helper(hosts, "remove", "a") == 0  # idempotent

    assert hosts.read_text() == OTHER + "# sandbox:b\n127.42.1.1\tmysql.b.test\n# end sandbox:b\n"


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["127.42.0.0/24", "mysql.jira-123.test=127.42.1.1"], "outside the set's block"),
        (["127.42.0.0/24", "mysql.jira-123.test=8.8.8.8"], "outside the set's block"),
        (["127.42.0.0/24", "github.com=127.42.0.1"], "does not end in .test"),
        (["127.42.0.0/24", "mysql.other.test=127.42.0.1"], "not a hostname under jira-123.test"),
        (["127.42.0.0/24", "a\n1.2.3.4 x.jira-123.test=127.42.0.1"], "not a hostname"),
        (["10.0.0.0/24", "mysql.jira-123.test=10.0.0.1"], "not a loopback /24"),
        (["127.0.0.0/8", "mysql.jira-123.test=127.0.0.2"], "not a loopback /24"),
        (["127.42.0.0/24", "mysql.jira-123.test"], "HOSTNAME=IP"),
    ],
)
def test_write_refuses_entries_outside_the_set(
    hosts: Path, capsys: pytest.CaptureFixture[str], args: list[str], reason: str
) -> None:
    status = helper(hosts, "write", "jira-123", *args)

    assert status != 0
    assert reason in capsys.readouterr().err
    assert hosts.read_text() == OTHER


def test_an_invalid_set_name_is_refused(hosts: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert helper(hosts, "remove", "../etc") != 0
    assert "invalid set name" in capsys.readouterr().err
    assert hosts.read_text() == OTHER


def test_an_unterminated_block_is_refused_rather_than_guessed(
    hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hosts.write_text(OTHER + "# sandbox:a\n127.42.0.1\tmysql.a.test\n10.1.1.1 keep.me\n")

    assert helper(hosts, "remove", "a") != 0

    assert "without a matching" in capsys.readouterr().err
    assert "keep.me" in hosts.read_text()
