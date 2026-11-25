"""Global config: defaults, `~` expansion, and field-level validation errors."""

from pathlib import Path

import pytest

from sandbox.config import Config, default_config_path, load_config
from sandbox.errors import SandboxError
from tests.conftest import Env, invoke
from tests.fakes import FakeGit


def test_defaults(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_CONFIG")
    home = Path.home()
    assert default_config_path() == home / ".config" / "sandbox" / "config.yaml"
    config = load_config()  # no file at the default location: all defaults
    assert config.templates_root == home / ".config" / "sandbox" / "plans"
    assert config.sets_root == home / "sandbox" / "sets"
    assert config.agent is None
    assert config.domain == "test"


def test_default_location_is_read(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_CONFIG")
    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        "domain: dev.test\nsets_root: ~/elsewhere\nagent: {services: {agent: {image: mine}}}\n"
    )
    config = load_config()
    assert config.domain == "dev.test"
    assert config.sets_root == Path.home() / "elsewhere"
    assert config.agent == {"services": {"agent": {"image": "mine"}}}


def test_empty_file_means_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("")
    assert load_config(path) == Config()


def test_explicitly_named_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="does not exist"):
        load_config(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("sets_root: relative/dir\n", "sets_root: Value error, must be an absolute path"),
        ("domian: dev.test\n", "domian: Extra inputs are not permitted"),
        ("agent: 3\n", "agent: Input should be a valid dictionary"),
        ("domain: local\n", "domain: Value error, 'local' is reserved for mDNS"),
        ("domain: sets.local\n", "domain: Value error, 'sets.local' ends in .local"),
        ("domain: Sandbox.Test\n", "domain: Value error, 'Sandbox.Test' is not a DNS suffix"),
        ("domain: .test\n", "domain: Value error, '.test' is not a DNS suffix"),
        ("domain: test.\n", "domain: Value error, 'test.' is not a DNS suffix"),
        ("domain: sand_box.test\n", "domain: Value error, 'sand_box.test' is not a DNS suffix"),
        ("domain: -sandbox.test\n", "domain: Value error, '-sandbox.test' is not a DNS suffix"),
        ('domain: ""\n', "domain: Value error, '' is not a DNS suffix"),
        ("domain: 3\n", "domain: Input should be a valid string"),
        # Too long to leave room for `<service>.<set>.` in a 253-character name.
        (f"domain: {'.'.join(['a' * 62, 'b' * 62, 'c'])}\n", "is not a DNS suffix"),
        ("- a\n- b\n", "(top level): Input should be a valid dictionary"),
    ],
)
def test_invalid_config_fails_with_field_level_errors(
    env: Env, content: str, expected: str
) -> None:
    env.config_file.write_text(content)
    env.make_repo_dir("api")
    git = FakeGit()
    git.add_repo(env.cwd / "api")
    result = invoke(["jira-123", "up", "--repos", "api"], git)
    assert result.exit_code == 1
    assert str(env.config_file) in result.stderr
    assert expected in result.stderr


def test_invalid_yaml_is_reported(env: Env) -> None:
    env.config_file.write_text("domain: [unclosed\n")
    result = invoke(["jira-123", "up", "--repos", "api"], FakeGit())
    assert result.exit_code == 1
    assert "invalid YAML" in result.stderr


@pytest.mark.parametrize("domain", ["test", "sandbox.test", "dev.sets.example", "x1.a-b.internal"])
def test_a_dns_suffix_of_one_or_more_labels_is_a_valid_domain(env: Env, domain: str) -> None:
    with env.config_file.open("a") as config:
        config.write(f"domain: {domain}\n")
    env.make_repo_dir("api")
    git = FakeGit()
    git.add_repo(env.cwd / "api")

    result = invoke(["jira-123", "up", "--repos", "api"], git)

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ip_pool", "127.42.0.0/16"),
        ("ip_pool", "banana"),
        ("hosts_file", "/etc/hosts"),
        ("hosts_file", "~/hosts"),
    ],
)
def test_a_key_of_the_loopback_model_fails_naming_it_as_no_longer_used(
    env: Env, key: str, value: str
) -> None:
    with env.config_file.open("a") as config:
        config.write(f"{key}: {value}\n")
    env.make_repo_dir("api")
    git = FakeGit()
    git.add_repo(env.cwd / "api")

    result = invoke(["jira-123", "up", "--repos", "api"], git)

    assert result.exit_code == 1
    assert str(env.config_file) in result.stderr
    assert f"{key}: no longer used" in result.stderr
    assert f"remove `{key}`" in result.stderr
    assert "Extra inputs" not in result.stderr
    assert not env.set_dir("jira-123").exists()


def test_both_keys_of_the_loopback_model_are_named_at_once(env: Env) -> None:
    with env.config_file.open("a") as config:
        config.write("ip_pool: 127.42.0.0/16\nhosts_file: /etc/hosts\n")

    result = invoke(["status"], FakeGit())

    assert result.exit_code == 1
    assert "ip_pool: no longer used" in result.stderr
    assert "hosts_file: no longer used" in result.stderr
