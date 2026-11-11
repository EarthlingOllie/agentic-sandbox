"""Agent overrides against real Docker: a custom image is probed with `docker run` before
any container starts, and the rendered override (with an extra service) is valid Compose.

Pulls `alpine:3.20` (has `sh`, lacks `git` and `claude`).
"""

import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

SET = unique_set("agent-override")
PROJECT = project_name(SET)
THIN_IMAGE = "alpine:3.20"
MYSQL = "services:\n  mysql:\n    image: mysql:8.4\n"


def project_containers() -> list[str]:
    return subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


def write_agent_override(env: Env, agent: dict[str, object]) -> None:
    with env.config_file.open("a") as handle:
        handle.write(yaml.safe_dump({"agent": agent}))


def test_image_without_git_and_claude_fails_before_any_container_starts(
    env: Env, make_repo: Callable[[str], Path]
) -> None:
    api = make_repo("api")
    (api / ".sandbox" / "compose.yaml").write_text(MYSQL)
    write_agent_override(env, {"services": {"agent": {"image": THIN_IMAGE}}})

    result = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 1, result.output
    assert f"agent image '{THIN_IMAGE}' has no git, claude on its PATH" in result.stderr
    assert project_containers() == []


def test_rendered_override_with_a_proxy_is_valid_compose(
    env: Env, make_repo: Callable[[str], Path]
) -> None:
    api = make_repo("api")
    (api / ".sandbox" / "compose.yaml").write_text(MYSQL)
    write_agent_override(
        env,
        {
            "services": {
                "agent": {
                    "environment": {"HTTPS_PROXY": "http://proxy:3128"},
                    "volumes": ["/var/run/docker.sock:/var/run/docker.sock"],
                    "user": "root",
                },
                "proxy": {"image": "ubuntu/squid:6.6-24.04_edge", "networks": ["egress"]},
            },
            "networks": {"egress": {}},
        },
    )
    # `up` renders the override; skipping the real `compose up` keeps this test container-free.
    compose = SubprocessCompose()
    invoke([SET, "up", "--repos", "api"], SubprocessGit(), _NoUp(compose))
    override = env.set_dir(SET) / "override.yaml"

    model = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(api / ".sandbox" / "compose.yaml"),
         "-f", str(override), "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout  # fmt: skip
    services = json.loads(model)["services"]
    assert services["agent"]["user"] != "root"
    assert services["agent"]["environment"]["HTTPS_PROXY"] == "http://proxy:3128"
    assert not any("docker.sock" in v.get("source", "") for v in services["agent"]["volumes"])
    assert set(services["proxy"]["networks"]) == {"default", "egress"}
    assert services["proxy"]["labels"]["sandbox.set"] == SET


class _NoUp:
    """The real Compose CLI for everything but `up`, so no container is started."""

    def __init__(self, compose: SubprocessCompose) -> None:
        self._compose = compose
        self.running_services = compose.running_services
        self.config = compose.config
        self.down = compose.down
        self.exec = compose.exec
        self.exec_captured = compose.exec_captured

    async def up(self, project: str, files: list[Path]) -> None:
        del project, files
