"""Templates against real git: `save --repos` then `up --from` from another directory."""

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git

pytestmark = pytest.mark.integration


def test_template_round_trip_pins_checked_out_branch_and_absolute_sources(
    env: Env, make_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_repo("api")
    web = make_repo("web")
    git(web, "checkout", "--quiet", "-b", "release/2.4")

    saved = invoke(["save", "--to", "stack", "--repos", "api", "web"], SubprocessGit())
    assert saved.exit_code == 0, saved.output
    template = yaml.safe_load((env.templates_root / "stack.yaml").read_text())
    assert template == {
        "repos": {"api": {"source": str(api.resolve())}, "web": {"source": str(web.resolve())}}
    }

    elsewhere = env.root / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = invoke(["jira-123", "up", "--from", "stack"], SubprocessGit())

    assert result.exit_code == 0, result.output
    set_dir = env.set_dir("jira-123")
    plan = yaml.safe_load((set_dir / "sandbox.yaml").read_text())
    assert plan == {
        "repos": {
            "api": {"source": str(api.resolve()), "base": "main"},
            "web": {"source": str(web.resolve()), "base": "release/2.4"},
        }
    }
    for repo in (api, web):
        assert git(set_dir / repo.name, "branch", "--show-current") == "agent/jira-123"

    # The set's plan saved back as a template keeps the pinned bases.
    resaved = invoke(["jira-123", "save", "--to", "pinned"], SubprocessGit())
    assert resaved.exit_code == 0, resaved.output
    assert yaml.safe_load((env.templates_root / "pinned.yaml").read_text()) == plan
