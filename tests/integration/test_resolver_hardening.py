"""The resolver against real Docker, as the one container every set can send packets to:
locked down, on the machine's loopback only, answering nothing but the domain, one of it
however many ``up``s start at once, and back by itself after a restart.

Lookups go straight to the resolver (``tests.dns``), never through the operating system.
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import tests.dns
from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.compose.resolver import (
    RESOLVER_ADDRESS,
    RESOLVER_CONTAINER,
    RESOLVER_LABEL,
    RESOLVER_PORT,
    RESOLVER_PROJECT,
)
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.dns import NOERROR, REFUSED, Answer, lookup
from tests.integration.conftest import git, resolver_networks, sandbox_argv, unique_set

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

A, B, C = unique_set("hard-a"), unique_set("hard-b"), unique_set("hard-c")
SETS = (A, B, C)

# How a test may restart the Docker daemon (`sudo systemctl restart docker` on a CI runner).
# Unset, the one test that needs it is skipped: restarting a developer's Docker stops
# everything they are running.
DOCKER_RESTART = "SANDBOX_TEST_DOCKER_RESTART"

# The port dnsmasq listens on inside the container, on every network it is attached to.
INSIDE_PORT = 5353

# A service that listens (HTTP on 8000), is up within a second, stops as fast, and comes
# back with Docker, as a set that is still running after a Docker restart must.
APP = """\
services:
  app:
    image: python:3.12-alpine
    command: ["python", "-m", "http.server", "8000"]
    stop_grace_period: 1s
    restart: always
"""


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def up(set_name: str, *options: str) -> None:
    result = invoke([set_name, "up", *options], SubprocessGit(), SubprocessCompose())
    assert result.exit_code == 0, result.output


def ask(name: str) -> Answer:
    return lookup(name, RESOLVER_ADDRESS, RESOLVER_PORT)


def network(set_name: str) -> str:
    return f"{project_name(set_name)}_default"


def app_container(set_name: str) -> str:
    return f"{project_name(set_name)}-app-1"


def address(set_name: str) -> str:
    """The app container's own address on its set's network, as Docker reports it now."""
    template = f'{{{{(index .NetworkSettings.Networks "{network(set_name)}").IPAddress}}}}'
    return docker("container", "inspect", "--format", template, app_container(set_name))


def inspect_resolver() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(docker("container", "inspect", RESOLVER_CONTAINER))[0])


def resolvers() -> list[str]:
    """Every container labelled as the resolver, running or not."""
    return docker("ps", "--all", "--quiet", "--filter", f"label={RESOLVER_LABEL}=true").split()


def remove_resolver() -> None:
    subprocess.run(
        ["docker", "compose", "-p", RESOLVER_PROJECT, "down", "--remove-orphans"],
        check=True,
        capture_output=True,
    )
    assert resolvers() == []


def resolves_again(set_names: tuple[str, ...], within: float) -> dict[str, tuple[str, ...]]:
    """Ask for each set's app until every answer is that container's current address, or
    ``within`` seconds have passed; the last answers either way. No sandbox command runs."""
    deadline = time.monotonic() + within
    answers: dict[str, tuple[str, ...]] = {}
    while True:
        for set_name in set_names:
            try:
                answers[set_name] = ask(f"app.{set_name}.test").addresses
            except OSError:  # nothing on the port yet: the resolver is still coming back
                answers[set_name] = ()
        if all(answers[s] and answers[s] == (current(s),) for s in set_names):
            return answers
        if time.monotonic() > deadline:
            return answers
        time.sleep(0.2)


def current(set_name: str) -> str:
    """Like :func:`address`, but empty while Docker (or the container) is away."""
    try:
        return address(set_name)
    except subprocess.CalledProcessError:
        return ""


def can_connect(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError:
        return False
    return True


def run_in(container: str, script: str) -> subprocess.CompletedProcess[str]:
    """Run a Python script inside ``container`` (the fixture's image has Python)."""
    return subprocess.run(
        ["docker", "exec", "-i", container, "python", "-"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )


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


def test_the_resolver_runs_without_privileges_a_writable_filesystem_or_mounts(repo: Path) -> None:
    up(A, "--repos", "api")

    container = inspect_resolver()
    host_config: dict[str, Any] = container["HostConfig"]
    # As it was started: every capability dropped and none added back (its port is an
    # unprivileged one, so binding it needs none), no-new-privileges, read-only, not root.
    assert host_config["CapDrop"] == ["ALL"]
    assert not host_config["CapAdd"]
    assert host_config["Privileged"] is False
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert host_config["ReadonlyRootfs"] is True
    assert container["Config"]["User"] == "65534:65534"
    # Nothing of the machine is mounted into it: no volume, no bind, no tmpfs, no socket.
    assert container["Mounts"] == []
    assert not host_config.get("Binds")
    assert not host_config.get("Tmpfs")
    assert not host_config.get("VolumesFrom")
    # As it runs: the dnsmasq process itself holds no capability and can gain none.
    status = dict(
        line.split(":\t", 1)
        for line in docker("exec", RESOLVER_CONTAINER, "cat", "/proc/1/status").splitlines()
    )
    assert status["Name"] == "dnsmasq"
    assert status["Uid"].split() == ["65534"] * 4
    for capabilities in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(status[capabilities], 16) == 0, capabilities
    assert status["NoNewPrivs"] == "1"
    write = subprocess.run(
        ["docker", "exec", RESOLVER_CONTAINER, "sh", "-c", "echo x > /etc/dnsmasq.d-probe"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert write.returncode != 0
    assert "Read-only file system" in write.stderr
    sockets = subprocess.run(
        ["docker", "exec", RESOLVER_CONTAINER, "sh", "-c",
         "test ! -e /var/run/docker.sock && test ! -e /run/docker.sock"],
        capture_output=True,
        check=False,
    )  # fmt: skip
    assert sockets.returncode == 0
    # And locked down like that, it does its job.
    assert ask(f"app.{A}.test").addresses == (address(A),)


def test_the_resolver_answers_on_the_machines_loopback_only_on_a_port_that_is_not_53(
    repo: Path,
) -> None:
    up(A, "--repos", "api")

    container = inspect_resolver()
    ports: dict[str, list[dict[str, str]] | None] = container["NetworkSettings"]["Ports"]
    published = {
        (protocol, binding["HostIp"], binding["HostPort"])
        for protocol, bindings in ports.items()
        for binding in bindings or []
    }
    assert published == {
        (f"{INSIDE_PORT}/udp", "127.0.0.1", "15353"),
        (f"{INSIDE_PORT}/tcp", "127.0.0.1", "15353"),
    }
    assert RESOLVER_PORT == 15353
    assert can_connect("127.0.0.1", RESOLVER_PORT)
    # Not on any other address of the machine: the one it reaches the outside world from
    # (when it has one), and the ones it has on Docker's networks, the resolver's included.
    elsewhere = {
        settings["Gateway"]
        for settings in cast(
            dict[str, dict[str, str]], container["NetworkSettings"]["Networks"]
        ).values()
    }
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # sends nothing: picks the outgoing address
            elsewhere.add(probe.getsockname()[0])
        except OSError:
            pass
    elsewhere.discard("127.0.0.1")
    assert len(elsewhere) >= 2
    for host_address in sorted(elsewhere):
        assert not can_connect(host_address, RESOLVER_PORT, timeout=1), host_address
        with pytest.raises(OSError):  # noqa: PT011 - refused (ICMP) or silence, never an answer
            lookup(f"app.{A}.test", host_address, RESOLVER_PORT, timeout=1)
    # Nothing sandbox runs holds port 53 on the machine.
    assert "53" not in {host_port for _, _, host_port in published}


def test_names_outside_the_domain_are_refused(repo: Path) -> None:
    up(A, "--repos", "api")
    assert ask(f"app.{A}.test").rcode == NOERROR

    outside = (
        "example.com",
        "localhost",
        "app",  # Docker's DNS would answer this short name; it is never asked
        app_container(A),  # nor a container name
        RESOLVER_CONTAINER,
        f"app.{A}.test.example.com",
        f"app.{A}.testing",  # ends in the domain's letters, is not under the domain
        "test.invalid",
    )
    for name in outside:
        answer = ask(name)
        assert answer.rcode == REFUSED, name
        assert answer.addresses == (), name
        assert answer.seconds < 1, name


def test_a_name_under_the_domain_is_never_forwarded_to_an_upstream_dns_server(
    repo: Path, env: Env
) -> None:
    """With a domain that exists on the public internet, a name under it that no container
    has gets no answer, where any upstream DNS server would have given one. Starting the
    resolver for another domain restarts it; other tests' ``up``s restart it for ``test``."""
    with env.config_file.open("a") as config:
        config.write("domain: example.com\n")
    up(A, "--repos", "api")
    try:
        assert ask(f"app.{A}.example.com").addresses == (address(A),)

        for name in ("www.example.com", "example.com"):
            answer = ask(name)
            assert answer.addresses == (), name
            assert answer.rcode != NOERROR, name
            assert answer.seconds < 1, name
        # Why: the only DNS server the container was given besides Docker's own is its own
        # loopback, where nothing listens, so Docker's DNS has nowhere to forward to.
        assert inspect_resolver()["HostConfig"]["Dns"] == ["127.0.0.1"]
        resolv_conf = docker("exec", RESOLVER_CONTAINER, "cat", "/etc/resolv.conf")
        nameservers = [
            line.split()[1] for line in resolv_conf.splitlines() if line.startswith("nameserver")
        ]
        assert nameservers == ["127.0.0.11"]
        listening = run_listening_check()
        assert listening == "closed"
    finally:
        # Hand the next test the resolver it expects, with nothing of this set on it.
        result = invoke([A, "down"], SubprocessGit(), SubprocessCompose())
        assert result.exit_code == 0, result.output


def run_listening_check() -> str:
    """Whether anything answers DNS on the resolver container's own loopback."""
    script = (
        "exec 3<>/dev/tcp/127.0.0.1/53 && echo open || echo closed; "
        f"exec 4<>/dev/tcp/127.0.0.1/{INSIDE_PORT} && echo open || echo closed"
    )
    result = subprocess.run(
        ["docker", "exec", RESOLVER_CONTAINER, "bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    states = set(result.stdout.split())
    assert len(states) == 1, result.stdout + result.stderr
    return states.pop()


def test_ups_started_at_the_same_moment_leave_one_resolver_attached_to_every_set(
    repo: Path,
) -> None:
    """Three real ``sandbox`` processes, with no resolver to begin with. This removes the
    resolver every set on the machine shares: a developer's own running sets resolve again
    after their next ``up``."""
    remove_resolver()
    sandbox = sandbox_argv()

    started = [
        subprocess.Popen(
            [*sandbox, set_name, "up", "--repos", "api"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for set_name in SETS
    ]
    outputs = [process.communicate(timeout=300)[0] for process in started]

    for process, output in zip(started, outputs, strict=True):
        assert process.returncode == 0, output
    # Exactly one resolver, running, started once and never replaced or restarted.
    assert len(resolvers()) == 1
    container = inspect_resolver()
    assert container["State"]["Running"] is True
    assert container["RestartCount"] == 0
    assert container["Name"] == f"/{RESOLVER_CONTAINER}"
    # On every one of those sets' networks, answering for each with its own container.
    assert {network(set_name) for set_name in SETS} <= resolver_networks()
    answers = {set_name: ask(f"app.{set_name}.test").addresses for set_name in SETS}
    assert answers == {set_name: (address(set_name),) for set_name in SETS}
    assert len(set(answers.values())) == len(SETS)


@pytest.mark.parametrize(
    "restart",
    [
        ["restart", RESOLVER_CONTAINER],
        # The resolver exiting by itself (dnsmasq ends on SIGTERM): nobody asks Docker for
        # anything, and Docker brings it back (`restart: always`).
        ["exec", RESOLVER_CONTAINER, "sh", "-c", "kill 1"],
    ],
    ids=["restarted", "exits-by-itself"],
)
def test_after_the_resolver_restarts_running_sets_resolve_again_without_sandbox(
    repo: Path, restart: list[str]
) -> None:
    for set_name in (A, B):
        up(set_name, "--repos", "api")
    before = inspect_resolver()

    docker(*restart)

    answers = resolves_again((A, B), within=20)
    assert answers == {A: (address(A),), B: (address(B),)}
    after = inspect_resolver()
    assert after["Id"] == before["Id"]
    assert after["State"]["StartedAt"] != before["State"]["StartedAt"]
    assert {network(A), network(B)} <= resolver_networks()


def test_after_docker_restarts_running_sets_resolve_again_without_sandbox(repo: Path) -> None:
    command = os.environ.get(DOCKER_RESTART)
    if not command:
        pytest.skip(
            f"restarting the Docker daemon is opt-in: set {DOCKER_RESTART} to the command "
            "that restarts it (e.g. 'sudo systemctl restart docker')"
        )
    for set_name in (A, B):
        up(set_name, "--repos", "api")
    before = {
        name: docker("container", "inspect", "--format", "{{.State.StartedAt}}", name)
        for name in (RESOLVER_CONTAINER, app_container(A), app_container(B))
    }

    subprocess.run(shlex.split(command), check=True, capture_output=True, timeout=300)

    # Docker starts the resolver and the sets' containers again and puts the resolver back
    # on the networks it was on. Nothing of sandbox runs between the restart and the lookups.
    answers = resolves_again((A, B), within=90)
    assert answers == {A: (address(A),), B: (address(B),)}
    assert answers[A] != answers[B]
    for name, started_at in before.items():
        now = docker("container", "inspect", "--format", "{{.State.StartedAt}}", name)
        assert now != started_at, f"{name} was not restarted: Docker did not restart"
    assert len(resolvers()) == 1
    assert {network(A), network(B)} <= resolver_networks()


def test_a_container_can_learn_another_sets_hostname_but_cannot_connect_to_it(
    repo: Path,
) -> None:
    for set_name in (A, B):
        up(set_name, "--repos", "api")
    other = address(B)
    # B's service is there and listening: the machine connects to it, and so does a
    # container of B's own set, by short name.
    assert can_connect(other, 8000)
    own = run_in(
        app_container(B),
        "import socket\nsocket.create_connection(('app', 8000), timeout=3).close()\n",
    )
    assert own.returncode == 0, own.stderr

    # A container in A that deliberately asks the resolver (at its address on A's own
    # network) is told where B's service is ...
    resolver_on_a = cast(
        dict[str, dict[str, str]], inspect_resolver()["NetworkSettings"]["Networks"]
    )[network(A)]["IPAddress"]
    name = f"app.{B}.test"
    learn = run_in(
        app_container(A),
        Path(tests.dns.__file__).read_text()
        + f"\nprint(' '.join(lookup({name!r}, {resolver_on_a!r}, {INSIDE_PORT}).addresses))\n",
    )
    assert learn.returncode == 0, learn.stderr
    assert learn.stdout.split() == [other]

    # ... and still cannot connect to it: not at the address it learnt, and not by name.
    for target in (other, name):
        attempt = run_in(
            app_container(A),
            "import socket\n"
            "try:\n"
            f"    socket.create_connection(({target!r}, 8000), timeout=3).close()\n"
            "except OSError as error:\n"
            "    print('blocked', type(error).__name__)\n"
            "else:\n"
            "    print('connected')\n",
        )
        assert attempt.returncode == 0, attempt.stderr
        assert attempt.stdout.split()[0] == "blocked", (target, attempt.stdout)
