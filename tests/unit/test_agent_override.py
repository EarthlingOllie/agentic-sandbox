"""Agent overrides from config, templates and set plans, checked through the rendered override.

Whatever an override says, sandbox's guarantees (mounts, user, ``CLAUDE_CONFIG_DIR``,
capability drops, no Docker socket) are re-applied on top of it, and a custom image must
have ``sh``, ``git`` and ``claude`` on PATH before any container starts.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from sandbox.cli import app
from sandbox.context import Deps
from tests.conftest import Env
from tests.fakes import FakeGit
from tests.fakes_compose import FakeCompose
from tests.fakes_images import FakeImageProbe

TOOLS = {"sh", "git", "claude"}
SET = "jira-123"


class Harness:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.git = FakeGit()
        self.compose = FakeCompose()
        self.images = FakeImageProbe(
            {"corp/agent:1": TOOLS, "corp/agent:2": TOOLS, "corp/agent:3": TOOLS}
        )

    def repo(self, name: str, compose: str | None = None) -> Path:
        path = self.env.make_repo_dir(name)
        (path / ".git").mkdir()
        self.git.add_repo(path)
        if compose is not None:
            (path / ".sandbox" / "compose.yaml").write_text(compose)
        return path

    def config(self, agent: dict[str, Any]) -> None:
        with self.env.config_file.open("a") as handle:
            handle.write(yaml.safe_dump({"agent": agent}))

    def invoke(self, *args: str) -> Result:
        deps = Deps(git=self.git, compose=self.compose, images=self.images)
        return CliRunner().invoke(app, list(args), obj=deps)

    def up(self, *args: str) -> Result:
        return self.invoke(SET, "up", *args)

    def override(self) -> dict[str, Any]:
        return yaml.safe_load((self.env.set_dir(SET) / "override.yaml").read_text())

    def agent(self) -> dict[str, Any]:
        return self.override()["services"]["agent"]

    def plan(self) -> dict[str, Any]:
        return yaml.safe_load((self.env.set_dir(SET) / "sandbox.yaml").read_text())


@pytest.fixture
def h(env: Env) -> Harness:
    return Harness(env)


def image_override(image: str, **agent: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"services": {"agent": {"image": image, **agent}}}


def targets(agent: dict[str, Any]) -> dict[str, str]:
    """Volume target -> source, for long and short syntax alike."""
    result: dict[str, str] = {}
    for volume in agent["volumes"]:
        if isinstance(volume, str):
            source, _, rest = volume.partition(":")
            result[rest.split(":")[0] or source] = source
        else:
            result[volume["target"]] = volume.get("source", "")
    return result


# --- precedence and pinning --------------------------------------------------------------


def test_config_override_is_pinned_into_the_set_plan(h: Harness) -> None:
    h.repo("api")
    h.config(image_override("corp/agent:1"))

    result = h.up("--repos", "api")

    assert result.exit_code == 0, result.output
    assert h.plan()["agent"] == image_override("corp/agent:1")
    assert h.agent()["image"] == "corp/agent:1"


def test_template_override_wins_over_config(h: Harness, env: Env) -> None:
    api = h.repo("api")
    h.config(image_override("corp/agent:1"))
    env.templates_root.mkdir(parents=True)
    (env.templates_root / "stack.yaml").write_text(
        yaml.safe_dump(
            {"repos": {"api": {"source": str(api)}}, **{"agent": image_override("corp/agent:2")}}
        )
    )

    result = h.up("--from", "stack")

    assert result.exit_code == 0, result.output
    assert h.plan()["agent"] == image_override("corp/agent:2")
    assert h.agent()["image"] == "corp/agent:2"


def test_template_without_override_falls_back_to_config(h: Harness, env: Env) -> None:
    api = h.repo("api")
    h.config(image_override("corp/agent:1"))
    env.templates_root.mkdir(parents=True)
    (env.templates_root / "stack.yaml").write_text(
        yaml.safe_dump({"repos": {"api": {"source": str(api)}}})
    )

    assert h.up("--from", "stack").exit_code == 0
    assert h.plan()["agent"] == image_override("corp/agent:1")


def test_set_plan_override_wins_and_later_config_changes_do_not_apply(h: Harness) -> None:
    h.repo("api")
    assert h.up("--repos", "api").exit_code == 0
    assert "agent" not in h.plan()
    assert h.invoke(SET, "down").exit_code == 0
    plan = h.plan()
    plan["agent"] = image_override("corp/agent:3")
    (h.env.set_dir(SET) / "sandbox.yaml").write_text(yaml.safe_dump(plan))
    h.config(image_override("corp/agent:1"))

    result = h.up()

    assert result.exit_code == 0, result.output
    assert h.agent()["image"] == "corp/agent:3"


def test_without_any_override_the_bundled_image_is_used_and_not_probed(h: Harness) -> None:
    h.repo("api")

    assert h.up("--repos", "api").exit_code == 0

    assert h.agent()["image"].startswith("sandbox-agent:")
    assert h.images.probed == []


# --- merging and the re-applied guarantees ------------------------------------------------


def test_custom_image_replaces_the_bundled_build_and_is_pulled(h: Harness) -> None:
    h.repo("api")
    h.config(image_override("corp/agent:1", environment={"HTTPS_PROXY": "http://proxy:3128"}))

    assert h.up("--repos", "api").exit_code == 0

    agent = h.agent()
    assert "build" not in agent
    assert "pull_policy" not in agent
    assert agent["environment"]["HTTPS_PROXY"] == "http://proxy:3128"
    assert agent["environment"]["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"


def test_override_cannot_remove_the_guarantees(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.getuid", lambda: 1234)
    monkeypatch.setattr("os.getgid", lambda: 5678)
    api = h.repo("api")
    h.config(
        image_override(
            "corp/agent:1",
            user="root",
            privileged=True,
            cap_add=["SYS_ADMIN"],
            cap_drop=[],
            security_opt=["seccomp=unconfined"],
            pid="host",
            network_mode="host",
            volumes_from=["mysql"],
            environment=["CLAUDE_CONFIG_DIR=/tmp/elsewhere", "HOME=/root", "EXTRA=1"],
            volumes=[
                "/var/run/docker.sock:/var/run/docker.sock",
                {"type": "bind", "source": "/run/docker.sock", "target": "/sock"},
                "/var/run:/host-run",
                "/srv/other:/workspace/jira-123",
                "cache:/home/agent/.cache",
            ],
        )
    )

    result = h.up("--repos", "api")

    assert result.exit_code == 0, result.output
    agent = h.agent()
    assert agent["user"] == "1234:5678"
    assert agent["cap_drop"] == ["ALL"]
    assert agent["security_opt"] == ["no-new-privileges:true"]
    for removed in ("privileged", "cap_add", "pid", "network_mode", "volumes_from"):
        assert removed not in agent
    assert agent["environment"] == {
        "EXTRA": "1",
        "HOME": "/home/agent",
        "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
    }
    mounted = targets(agent)
    assert not any("docker.sock" in source for source in mounted.values())
    assert "/host-run" not in mounted
    assert mounted["/workspace/jira-123"] == str(h.env.set_dir(SET))
    assert mounted[f"{api}/.git"] == f"{api}/.git"
    assert mounted["/home/agent/.claude"] == str(Path.home() / ".claude")
    assert mounted["/home/agent/.cache"] == "cache"  # harmless extras are kept
    assert agent["labels"]["sandbox.set"] == SET
    # Each overridden setting is reported, not silently dropped.
    for word in ("docker.sock", "user", "privileged", "cap_add", "network_mode"):
        assert word in result.stderr


def test_override_without_an_image_keeps_the_bundled_image(h: Harness) -> None:
    h.repo("api")
    h.config({"services": {"agent": {"environment": {"FOO": "bar"}}}})

    assert h.up("--repos", "api").exit_code == 0

    agent = h.agent()
    assert agent["image"].startswith("sandbox-agent:")
    assert agent["pull_policy"] == "never"
    assert agent["environment"]["FOO"] == "bar"
    assert h.images.probed == []


# --- extra services ----------------------------------------------------------------------


PROXY = {
    "image": "ubuntu/squid:6.6",
    "networks": ["egress"],
    "network_mode": "bridge",
}


def test_extra_services_join_the_set_network_and_are_labelled(h: Harness) -> None:
    h.repo("api", "services:\n  mysql:\n    image: mysql:8.4\n")
    h.config(
        {
            "services": {
                "agent": {"environment": {"HTTPS_PROXY": "http://proxy:3128"}},
                "proxy": PROXY,
                "dns": {"image": "coredns/coredns:1.11.1"},
            },
            "networks": {"egress": {}},
            "volumes": {"squid-cache": {}},
        }
    )

    result = h.up("--repos", "api")

    assert result.exit_code == 0, result.output
    override = h.override()
    proxy = override["services"]["proxy"]
    assert proxy["image"] == "ubuntu/squid:6.6"
    assert sorted(proxy["networks"]) == ["default", "egress"]
    assert "network_mode" not in proxy
    assert proxy["labels"]["sandbox.set"] == SET
    # A service without `networks` is on the default (set) network already.
    assert "networks" not in override["services"]["dns"]
    assert override["networks"]["egress"]["labels"]["sandbox.set"] == SET
    assert override["volumes"]["squid-cache"]["labels"]["sandbox.set"] == SET
    assert "proxy" in result.stdout


def test_extra_service_may_not_shadow_a_repo_service(h: Harness) -> None:
    h.repo("api", "services:\n  mysql:\n    image: mysql:8.4\n")
    h.config({"services": {"mysql": {"image": "mysql:5.7"}}})

    result = h.up("--repos", "api")

    assert result.exit_code == 1
    assert "mysql" in result.stderr
    assert h.compose.calls == []


# --- image check -------------------------------------------------------------------------


def test_custom_image_missing_tools_fails_before_any_container_starts(h: Harness) -> None:
    h.repo("api", "services:\n  mysql:\n    image: mysql:8.4\n")
    h.images.images["corp/thin:1"] = {"sh"}
    h.config(image_override("corp/thin:1"))

    result = h.up("--repos", "api")

    assert result.exit_code == 1
    assert "corp/thin:1" in result.stderr
    assert "git, claude" in result.stderr
    assert h.compose.calls == []
    assert not (h.env.set_dir(SET) / "api").exists()


def test_custom_image_that_cannot_run_fails_before_any_container_starts(h: Harness) -> None:
    h.repo("api")
    h.config(image_override("corp/missing:1"))

    result = h.up("--repos", "api")

    assert result.exit_code == 1
    assert "corp/missing:1" in result.stderr
    assert h.compose.calls == []


# --- fragment validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "message"),
    [
        ({"image": "corp/agent:1"}, "unknown key 'image'"),
        ({"services": {"agent": {"build": "."}}}, "build is not supported"),
        ({"services": {"agent": "corp/agent:1"}}, "services.agent must be a mapping"),
        ({"services": []}, "services must be a mapping"),
    ],
)
def test_invalid_fragments_fail_with_field_level_errors(
    h: Harness, agent: dict[str, Any], message: str
) -> None:
    h.repo("api")
    h.config(agent)

    result = h.up("--repos", "api")

    assert result.exit_code == 1
    assert "agent" in result.stderr
    assert message in result.stderr
