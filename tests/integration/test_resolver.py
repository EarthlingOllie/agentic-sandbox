"""The resolver against real Docker: asked directly on its loopback port, it answers each
set hostname with that container's own address, for as long as the set is up.

Lookups go straight to the resolver (``tests.dns``), never through the operating system:
pointing the machine at the resolver is ``sudo sandbox setup``'s job.
"""

import json
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.compose.resolver import (
    IMAGE_REPOSITORY,
    RESOLVER_ADDRESS,
    RESOLVER_CONTAINER,
    RESOLVER_PORT,
    resolver_image,
)
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.dns import NOERROR, REFUSED, Answer, lookup
from tests.integration.conftest import git, resolver_networks, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

A, B = unique_set("res-a"), unique_set("res-b")
SETS = (A, B)

# A service that is up within a second and stops as fast.
APP = """\
services:
  app:
    image: python:3.12-alpine
    command: ["sleep", "infinity"]
    stop_grace_period: 1s
"""
UNHEALTHY = """\
    healthcheck:
      test: ["CMD", "false"]
      interval: 1s
      timeout: 2s
      retries: 2
      start_period: 0s
"""


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def run(*args: str) -> tuple[int, str]:
    result = invoke(list(args), SubprocessGit(), SubprocessCompose())
    return result.exit_code, result.output


def up(set_name: str, *options: str) -> None:
    code, output = run(set_name, "up", *options)
    assert code == 0, output


def ask(name: str) -> Answer:
    return lookup(name, RESOLVER_ADDRESS, RESOLVER_PORT)


def network(set_name: str) -> str:
    return f"{project_name(set_name)}_default"


def address(set_name: str, service: str = "app") -> str:
    """The service container's own address on its set's network, as Docker reports it."""
    container = f"{project_name(set_name)}-{service}-1"
    template = f'{{{{(index .NetworkSettings.Networks "{network(set_name)}").IPAddress}}}}'
    found = docker("container", "inspect", "--format", template, container)
    assert found, container
    return found


def networks() -> list[str]:
    return docker("network", "ls", "--format", "{{.Name}}").split()


def stop_containers(set_name: str) -> None:
    ids = docker("ps", "-q", "--filter", f"label=sandbox.set={set_name}").split()
    docker("stop", "-t", "1", *ids)


@pytest.fixture
def repo(env: Env) -> Iterator[Path]:
    path = env.make_repo_dir("api")
    (path / ".sandbox" / "compose.yaml").write_text(APP)
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    yield path
    for set_name in SETS:
        subprocess.run(
            ["docker", "compose", "-p", project_name(set_name), "down", "--volumes"],
            capture_output=True,
            check=False,
        )


def test_two_sets_resolve_side_by_side_each_name_to_its_own_container(repo: Path) -> None:
    for set_name in SETS:
        up(set_name, "--repos", "api")

    answers = {set_name: ask(f"app.{set_name}.test") for set_name in SETS}

    for set_name, answer in answers.items():
        assert answer.rcode == NOERROR
        assert answer.addresses == (address(set_name),)
        # Docker answers with a 600-second TTL; the resolver hands out at most 1 second.
        assert answer.ttl is not None
        assert answer.ttl <= 1
    assert answers[A].addresses != answers[B].addresses
    assert {network(A), network(B)} <= resolver_networks()


def test_the_resolver_is_built_from_the_bundled_definition_and_belongs_to_no_set(
    repo: Path,
) -> None:
    up(A, "--repos", "api")

    container = cast(
        dict[str, Any], json.loads(docker("container", "inspect", RESOLVER_CONTAINER))[0]
    )
    image: str = container["Config"]["Image"]
    labels: dict[str, str] = container["Config"]["Labels"]
    # The content-addressed tag of the bundled definition, which Compose builds and never
    # pulls (tests/unit/test_resolver_definition.py).
    assert image == resolver_image()
    repository, _, tag = image.partition(":")
    assert repository == IMAGE_REPOSITORY
    assert re.fullmatch(r"[0-9a-f]{12}", tag)
    assert labels["sandbox.resolver"] == "true"
    assert labels["com.docker.compose.project"] == "sandbox_resolver"
    assert "sandbox.set" not in labels
    # Published on the machine's loopback only.
    ports: dict[str, list[dict[str, str]] | None] = container["NetworkSettings"]["Ports"]
    published = [binding for bindings in ports.values() for binding in bindings or []]
    assert published
    assert {binding["HostIp"] for binding in published} == {"127.0.0.1"}
    assert {binding["HostPort"] for binding in published} == {str(RESOLVER_PORT)}
    # No set shows up for it.
    code, output = run("status", "--json")
    assert code == 0, output
    assert "resolver" not in [s["name"] for s in json.loads(output)["sets"]]


def test_only_names_of_running_containers_under_the_domain_are_answered(repo: Path) -> None:
    up(A, "--repos", "api")
    assert ask(f"app.{A}.test").addresses

    # A name under the domain that no container has: an immediate failure, not a timeout.
    unknown = ask(f"nope.{A}.test")
    assert unknown.addresses == ()
    assert unknown.rcode != NOERROR
    assert unknown.seconds < 1
    # Names outside the domain are not forwarded anywhere, not even to Docker's DNS, which
    # knows the service by its short name.
    for name in ("app", f"app.{A}.example", "example.com"):
        outside = ask(name)
        assert outside.addresses == (), name
        assert outside.rcode == REFUSED, name
        assert outside.seconds < 1, name


def test_recreate_resolves_to_the_new_container_within_two_seconds(repo: Path) -> None:
    up(A, "--repos", "api")
    name = f"app.{A}.test"
    container = f"{project_name(A)}-app-1"
    before = docker("container", "inspect", "--format", "{{.Id}}", container)
    assert ask(name).addresses == (address(A),)

    up(A, "--recreate")
    finished = time.monotonic()

    assert docker("container", "inspect", "--format", "{{.Id}}", container) != before
    current = address(A)
    answer = ask(name)
    while answer.addresses != (current,) and time.monotonic() - finished < 2:
        time.sleep(0.1)
        answer = ask(name)
    assert answer.addresses == (current,)


def test_down_stops_the_sets_names_resolving_and_detaches_the_resolver(repo: Path) -> None:
    for set_name in SETS:
        up(set_name, "--repos", "api")

    code, output = run(A, "down")

    assert code == 0, output
    gone = ask(f"app.{A}.test")
    assert gone.addresses == ()
    assert gone.seconds < 1
    assert network(A) not in resolver_networks()
    assert network(A) not in networks()
    # The other set is untouched.
    assert ask(f"app.{B}.test").addresses == (address(B),)


def test_a_rolled_back_up_stops_the_sets_names_resolving_and_detaches_the_resolver(
    repo: Path,
) -> None:
    up(A, "--repos", "api")
    assert network(A) in resolver_networks()
    # The set stops half way and its service can no longer turn healthy: the next `up`
    # resumes, fails and rolls back with the resolver still on the set's network.
    docker("stop", "-t", "1", f"{project_name(A)}-app-1")
    (repo / ".sandbox" / "compose.yaml").write_text(APP + UNHEALTHY)

    code, output = run(A, "up")

    assert code == 1
    assert f"rolled back set '{A}'" in output
    assert ask(f"app.{A}.test").addresses == ()
    assert network(A) not in resolver_networks()
    assert network(A) not in networks()
    assert docker("ps", "-a", "-q", "--filter", f"label=sandbox.set={A}") == ""
    running = docker("container", "inspect", "--format", "{{.State.Running}}", RESOLVER_CONTAINER)
    assert running == "true"


@pytest.mark.parametrize("verb", [["prune"], ["destroy", "--force"]], ids=["prune", "destroy"])
def test_clearing_one_set_leaves_the_resolver_running_and_other_sets_resolving(
    repo: Path, verb: list[str]
) -> None:
    for set_name in SETS:
        up(set_name, "--repos", "api")
    # Stopped without `down`: its containers and network, with the resolver on it, are left.
    stop_containers(A)

    code, output = run(A, *verb)

    assert code == 0, output
    assert network(A) not in networks()
    assert network(A) not in resolver_networks()
    assert docker("ps", "-a", "-q", "--filter", f"label=sandbox.set={A}") == ""
    running = docker("container", "inspect", "--format", "{{.State.Running}}", RESOLVER_CONTAINER)
    assert running == "true"
    assert ask(f"app.{B}.test").addresses == (address(B),)
