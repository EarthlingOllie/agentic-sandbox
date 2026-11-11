"""The whole product as a user runs it, privileged parts included.

``sudo sandbox setup`` → two sets ``up`` in parallel, each with two repos sharing ``mysql``
(one ships an init hook) and a web app with an extra host → HTTPS by hostname through the
real ``/etc/hosts`` and the installed mkcert CA from the host, and over the set network
from the agent → the agent commits in its clone → ``down`` brings that commit into the repo
as ``agent/<set>`` → ``destroy`` → ``sudo sandbox setup --uninstall``.
"""

import json
import re

import pytest

from sandbox.compose import project_name
from sandbox.hosts_block import HELPER_PATH, SYSTEM_HOSTS_FILE, block_start
from sandbox.setup import SUDOERS_PATH
from tests.e2e.conftest import E2E_ENABLED, SKIP_REASON, Machine

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not E2E_ENABLED, reason=SKIP_REASON)]

REPOS = ("api", "jobs", "web")


def hosts_text() -> str:
    return SYSTEM_HOSTS_FILE.read_text(encoding="utf-8")


def binding_ip(output: str, hostname: str) -> str:
    """The IP ``up`` printed for ``hostname`` (``<hostname>  <ip>  <ports>``)."""
    match = re.search(rf"^\s*{re.escape(hostname)}  (\S+)  ", output, re.MULTILINE)
    assert match, f"no binding for {hostname} in:\n{output}"
    return match.group(1)


def curl_from_host(machine: Machine, hostname: str) -> dict[str, object]:
    # No --resolve, no --cacert, no -k: the name comes from /etc/hosts and trust from the
    # CA setup installed. --noproxy keeps a runner's proxy away from the .test names.
    done = machine.command(
        ["curl", "-sS", "--fail", "--noproxy", "*", f"https://{hostname}/"]
    ).check()
    return json.loads(done.output)


def in_container(machine: Machine, set_name: str, service: str, *command: str) -> str:
    argv = ["docker", "compose", "-p", project_name(set_name), "exec", "-T", service, *command]
    return machine.command(argv).check().output


def services(machine: Machine, set_name: str, *flags: str) -> list[str]:
    argv = ["docker", "compose", "-p", project_name(set_name), "ps", *flags, "--services"]
    return machine.command(argv).check().output.split()


def assert_set_works(machine: Machine, set_name: str, up_output: str) -> str:
    """Checks one running set end to end; returns the web app's IP."""
    web, admin = f"web.{set_name}.test", f"admin.{set_name}.test"
    ip = binding_ip(up_output, web)
    assert binding_ip(up_output, admin) == ip

    # The set's block in the real hosts file, resolved by the system resolver.
    assert block_start(set_name) in hosts_text()
    resolved = machine.command(["getent", "hosts", web]).check().output.split()
    assert resolved[0] == ip

    # The two repos share one mysql; the init hook from `jobs` ran inside it.
    assert services(machine, set_name).count("mysql") == 1
    databases = in_container(
        machine, set_name, "mysql",
        "mysql", "-uroot", "-psandbox", "-N", "-e", "SHOW DATABASES LIKE 'web_app'",
    )  # fmt: skip
    assert "web_app" in databases.split()

    expected = {
        "user": 65534,
        "WORKTREE": str(machine.sets_root / set_name / "web"),
        "APP_URL": f"https://{web}",
    }
    for hostname in (web, admin):
        assert curl_from_host(machine, hostname) == expected
        from_agent = in_container(
            machine, set_name, "agent", "curl", "-sS", "--fail", f"https://{hostname}/"
        )
        assert json.loads(from_agent) == expected
    return ip


def test_setup_up_https_down_destroy_uninstall(machine: Machine) -> None:
    # setup: the helper, its sudoers rule and the mkcert CA.
    installed = machine.run("setup", sudo=True).check()
    assert str(HELPER_PATH) in installed.output
    assert HELPER_PATH.is_file()
    assert machine.command(["sudo", "-n", "-l", str(HELPER_PATH)]).status == 0

    # up: two sets at once, each with every repo.
    first, second = machine.set_name("a"), machine.set_name("b")
    started = [machine.start(name, "up", "--repos", *REPOS) for name in (first, second)]
    up = [running.wait().check() for running in started]

    # Both work, side by side, on their own IPs (mysql:3306 is published by both).
    ips = [assert_set_works(machine, name, done.output) for name, done in zip(
        (first, second), up, strict=True
    )]  # fmt: skip
    assert ips[0] != ips[1]
    mysql = [binding_ip(done.output, f"mysql.{name}.test") for name, done in zip(
        (first, second), up, strict=True
    )]  # fmt: skip
    assert mysql[0] != mysql[1]

    # The agent commits in its own clone; the repo's agent/<set> doesn't move until synced.
    agent_commit = (
        "cd api && echo work > agent.txt && git add agent.txt"
        " && git -c user.name=agent -c user.email=agent@example.invalid commit -q -m 'agent work'"
    )
    in_container(machine, first, "agent", "sh", "-c", agent_commit)

    # down: containers stop, the hosts block goes, the clones stay, agent/<set> is synced.
    for name in (first, second):
        machine.run(name, "down").check()
        assert services(machine, name, "--all") == []
        assert block_start(name) not in hosts_text()
        assert (machine.sets_root / name / "web").is_dir()
    assert machine.command(["getent", "hosts", f"web.{first}.test"]).status != 0
    log = ["git", "-C", str(machine.code / "api"), "log", "-1", "--format=%s", f"agent/{first}"]
    assert machine.command(log).check().output.strip() == "agent work"

    # destroy: the clones, branches (the agent's commit is unpushed: --force) and set dir.
    for name, flags in ((first, ["--force"]), (second, [])):
        machine.run(name, "destroy", "--branches", *flags).check()
        assert not (machine.sets_root / name).exists()
        for repo in REPOS:
            source = machine.code / repo
            branches = machine.command(["git", "-C", str(source), "branch", "--list"])
            assert f"agent/{name}" not in branches.check().output

    # setup --uninstall: the helper and its rule are gone, so `up` refuses again.
    removed = machine.run("setup", "--uninstall", sudo=True).check()
    assert "mkcert -uninstall" in removed.output
    assert not HELPER_PATH.exists()
    assert machine.command(["sudo", "-n", "test", "-e", str(SUDOERS_PATH)]).status != 0
    refused = machine.run(machine.set_name("c"), "up", "--repos", *REPOS)
    assert refused.status != 0
    assert "sudo sandbox setup" in refused.output
