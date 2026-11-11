"""Global config: defaults, `~` expansion, and field-level validation errors."""

from ipaddress import IPv4Network
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
    assert config.ip_pool == IPv4Network("127.42.0.0/16")
    assert config.templates_root == home / ".config" / "sandbox" / "plans"
    assert config.sets_root == home / "sandbox" / "sets"
    assert config.agent is None


def test_default_location_is_read(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_CONFIG")
    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        "ip_pool: 127.50.0.0/16\nsets_root: ~/elsewhere\n"
        "agent: {services: {agent: {image: mine}}}\n"
    )
    config = load_config()
    assert config.ip_pool == IPv4Network("127.50.0.0/16")
    assert config.sets_root == Path.home() / "elsewhere"
    assert config.agent == {"services": {"agent": {"image": "mine"}}}


def test_empty_file_means_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("")
    assert load_config(path).ip_pool == Config().ip_pool


def test_explicitly_named_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="does not exist"):
        load_config(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("ip_pool: 10.0.0.0/16\n", "ip_pool: Value error, must be inside 127.0.0.0/8"),
        ("ip_pool: 127.42.0.0/25\n", "ip_pool: Value error, must be a /24 or larger"),
        ("ip_pool: banana\n", "ip_pool: Input is not a valid IPv4 network"),
        ("sets_root: relative/dir\n", "sets_root: Value error, must be an absolute path"),
        ("ip_pol: 127.42.0.0/16\n", "ip_pol: Extra inputs are not permitted"),
        ("agent: 3\n", "agent: Input should be a valid dictionary"),
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
    env.config_file.write_text("ip_pool: [unclosed\n")
    result = invoke(["jira-123", "up", "--repos", "api"], FakeGit())
    assert result.exit_code == 1
    assert "invalid YAML" in result.stderr
