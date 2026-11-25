"""The agent container against real Docker: hardened, mounts, host UID, `git` in its clone,
and its set hostname, `agent.<set>.<domain>`, from the machine and from inside the set.

The first run builds the default agent image (Compose builds it on `up`), which pulls
`ubuntu:24.04` and installs `claude` from npm.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import container_address, git, resolve, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

SET = unique_set("agent")
PROJECT = project_name(SET)
CONTAINER = f"{PROJECT}-agent-1"
RUNNING_AS_ROOT = os.getuid() == 0


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def in_agent(script: str, *, workdir: str | None = None) -> str:
    """Run ``script`` in the agent the way `sandbox sh` does (`compose exec`)."""
    where = ["-w", workdir] if workdir else []
    return docker("compose", "-p", PROJECT, "exec", "-T", *where, "agent", "sh", "-c", script)


@pytest.fixture
def api(env: Env, make_repo: Callable[[str], Path]) -> Iterator[Path]:
    path = make_repo("api")
    result = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())
    assert result.exit_code == 0, result.output
    yield path
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


def test_agent_is_hardened_and_ships_the_tools(api: Path) -> None:
    uid, gid = (os.getuid(), os.getgid()) if not RUNNING_AS_ROOT else (1000, 1000)
    assert in_agent("id -u; id -g").split() == [str(uid), str(gid)]
    assert in_agent("whoami")  # the entrypoint added a passwd entry for the host UID
    status = in_agent("grep -E '^(CapEff|CapPrm|NoNewPrivs)' /proc/self/status")
    assert "CapEff:\t0000000000000000" in status
    assert "CapPrm:\t0000000000000000" in status
    assert "NoNewPrivs:\t1" in status
    for tool in ("sh", "git", "claude"):
        assert in_agent(f"command -v {tool}")
    assert "Claude Code" in in_agent("claude --version")
    assert in_agent("test ! -e /var/run/docker.sock && test ! -e /run/docker.sock && echo ok")
    binds = docker("inspect", "-f", "{{range .Mounts}}{{.Source}}\n{{end}}", CONTAINER)
    assert "docker.sock" not in binds


def test_agent_sees_the_workspace_and_git_dirs_but_not_the_main_working_tree(
    env: Env, api: Path
) -> None:
    assert in_agent("pwd") == f"/workspace/{SET}"
    assert in_agent("test -f api/.sandbox/compose.yaml && echo ok") == "ok"
    assert in_agent(f"test -d {api}/.git && echo ok") == "ok"
    assert in_agent(f"test ! -e {api}/.sandbox && echo ok") == "ok"
    # The plan pins the agent override, so the agent can read it but not change it.
    assert in_agent("test -r sandbox.yaml && echo ok") == "ok"
    assert in_agent("echo x >> sandbox.yaml 2>/dev/null || echo refused") == "refused"
    assert in_agent("rm -f sandbox.yaml 2>/dev/null || echo refused") == "refused"


def test_host_claude_dir_is_shared(env: Env, api: Path) -> None:
    (Path.home() / ".claude" / "from-host").write_text("hi")
    assert in_agent('echo "$CLAUDE_CONFIG_DIR"') == "/home/agent/.claude"
    assert in_agent("cat /home/agent/.claude/from-host") == "hi"
    assert in_agent("test ! -e /home/agent/.claude.json && echo ok") == "ok"


@pytest.mark.skipif(
    RUNNING_AS_ROOT, reason="the agent never runs as root, so it can't write root's files"
)
def test_git_works_in_the_clone_and_files_stay_the_developers(env: Env, api: Path) -> None:
    clone = f"/workspace/{SET}/api"
    assert in_agent("git branch --show-current", workdir=clone) == f"agent/{SET}"
    in_agent(
        "echo change > made-by-agent.txt && git add made-by-agent.txt"
        " && git -c user.name=agent -c user.email=agent@example.invalid"
        " commit --quiet -m 'from the agent'",
        workdir=clone,
    )
    in_agent("echo hi > /home/agent/.claude/written-by-agent")

    on_host = env.set_dir(SET) / "api" / "made-by-agent.txt"
    for path in (on_host, Path.home() / ".claude" / "written-by-agent"):
        assert (path.stat().st_uid, path.stat().st_gid) == (os.getuid(), os.getgid())
    # Synced into the source repo (by `status` here) as the agent's branch.
    synced = invoke([SET, "status"], SubprocessGit(), SubprocessCompose())
    assert synced.exit_code == 0, synced.output
    assert git(api, "log", "-1", "--format=%s", f"agent/{SET}") == "from the agent"


# --- the agent's set hostname ------------------------------------------------------------

PORT = 8080
SERVE = (
    "require('http').createServer((request, response) => response.end('from the agent'))"
    f".listen({PORT}, '0.0.0.0')"
)
# An agent override that puts the agent on a second network and adds a service to the set.
OVERRIDE: dict[str, Any] = {
    "services": {
        "agent": {"networks": ["egress"]},
        "probe": {
            "image": "python:3.12-alpine",
            "command": ["sleep", "infinity"],
            "stop_grace_period": "1s",
        },
    },
    "networks": {"egress": {}},
}
FETCH = (
    "import sys, urllib.request; "
    "print(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())"
)


def serve_in_agent() -> None:
    """Start a web server in the agent, the way an agent would: a process it leaves running."""
    docker("compose", "-p", PROJECT, "exec", "-d", "agent", "node", "-e", SERVE)


def fetch(url: str) -> str:
    """GET ``url`` from this machine, waiting out the moment the listener needs to start."""
    deadline = time.monotonic() + 10
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - http
                return response.read().decode()
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def test_a_listener_in_the_agent_is_reached_at_the_address_its_hostname_resolves_to(
    api: Path,
) -> None:
    name = f"agent.{SET}.test"
    serve_in_agent()

    # The resolver, asked for the name, answers with the agent container's own address...
    addresses = resolve(name)
    assert addresses == (container_address(SET, "agent"),)
    # ...and this machine reaches what the agent serves there. (The test names the address:
    # sending the operating system's lookups to the resolver is `sudo sandbox setup`'s job.)
    assert fetch(f"http://{addresses[0]}:{PORT}/") == "from the agent"

    # `status` and `urls` show the same hostname and address.
    status = invoke([SET, "status", "--json"], SubprocessGit(), SubprocessCompose())
    assert status.exit_code == 0, status.output
    (agent,) = (s for s in json.loads(status.stdout)["services"] if s["name"] == "agent")
    assert (agent["hostname"], agent["ip"]) == (name, addresses[0])
    urls = invoke([SET, "urls", "--json"], SubprocessGit(), SubprocessCompose())
    assert urls.exit_code == 0, urls.output
    assert json.loads(urls.stdout)["agent"] == {
        "address": name,
        "service": "agent",
        "ip": addresses[0],
    }


def test_the_agents_hostname_no_longer_resolves_once_the_set_is_down(api: Path) -> None:
    name = f"agent.{SET}.test"
    assert resolve(name)

    result = invoke([SET, "down"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    assert resolve(name) == ()


def test_with_an_agent_override_the_agent_answers_by_name_from_inside_the_set(
    env: Env, make_repo: Callable[[str], Path]
) -> None:
    make_repo("api")
    with env.config_file.open("a") as config:
        config.write(yaml.safe_dump({"agent": OVERRIDE}))
    name = f"agent.{SET}.test"
    try:
        result = invoke([SET, "up", "--repos", "api"], SubprocessGit(), SubprocessCompose())
        assert result.exit_code == 0, result.output
        serve_in_agent()
        on_set_network = container_address(SET, "agent")
        assert fetch(f"http://{on_set_network}:{PORT}/") == "from the agent"

        # Another container of the set opens it by name...
        from_inside = docker(
            "compose", "-p", PROJECT, "exec", "-T", "probe",
            "python", "-c", FETCH, f"http://{name}:{PORT}/",
        )  # fmt: skip
        assert from_inside == "from the agent"
        # ...the agent knows itself by that name...
        assert on_set_network in in_agent(f"getent hosts {name}")
        # ...and the resolver answers the set network's address, not the override network's.
        assert resolve(name) == (on_set_network,)
    finally:
        subprocess.run(
            ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
            capture_output=True,
            check=False,
        )
