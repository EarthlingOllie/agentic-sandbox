"""The walkthrough harness: the installed ``sandbox`` CLI through every verb, with a report.

One opt-in command::

    SANDBOX_E2E=1 uv run pytest -m e2e tests/e2e
    SANDBOX_E2E=1 SANDBOX_E2E_KEEP_SETUP=1 uv run pytest -m e2e tests/e2e   # keep the setup

It runs ``setup``, ``up --repos``, ``save`` (from repos and from a set), ``templates``,
``up --from``, a repeated ``up``, ``up --recreate``, ``status``, ``SET status`` and ``urls``
(each also with ``--json``), ``sh``, ``sh --repo``, ``sh SERVICE``, ``claude``, bare
``prune``, ``down``, ``destroy`` (its refusals, ``--force``, ``--branches``), ``SET prune``
and ``setup --uninstall``. Each step checks the exit status and what the verb promises, and
every check goes into a report printed at the end (``tests.e2e.walkthrough``): the command,
what was checked, pass or fail, and the output of anything that failed. A failed check does
not stop the steps that do not depend on it, and the teardown always runs.

Set hostnames are resolved the way any program on the machine resolves them, through the
operating system (``getaddrinfo``), never by asking the resolver directly.
"""

import json
import os
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from sandbox.compose import project_name
from sandbox.compose.resolver import RESOLVER_CONTAINER
from sandbox.plan import plan_path
from sandbox.platforms import Host
from sandbox.resolver_config import installed_domains
from tests.e2e.conftest import E2E_ENABLED, REPORT_KEY, RUN_ID, SKIP_REASON, Completed, Machine
from tests.e2e.walkthrough import Phase, Stop, Walkthrough

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not E2E_ENABLED, reason=SKIP_REASON)]

REPOS = ("api", "jobs", "web")
# Every service of a set built from REPOS: the agent, the `mysql` that `api` and `jobs`
# share, `toolbox` from `jobs` (it declares no ports) and the web app.
SERVICES = ["agent", "mysql", "toolbox", "web"]
# The web app's extra host: another name of the `web` container.
EXTRA_HOSTS = {"admin": "web"}
# How long the operating system may take to follow the resolver (its answers live 1 second).
RESOLVE_WITHIN = 20.0
PROJECT_LABEL = "com.docker.compose.project"
CLAUDE_VERSION = re.compile(r"^\d+\.\d+\.\d+\S* \(Claude Code\)$", re.MULTILINE)


def that(condition: object, problem: str) -> None:
    """A check's assertion: ``problem`` is what the report shows when it does not hold."""
    if not condition:
        raise AssertionError(problem)


def lookup(hostname: str) -> str | None:
    """The address the operating system's resolver gives for ``hostname``; None if none."""
    try:
        found = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    return str(found[0][4][0])


def eventually(found: Callable[[], object], wanted: Callable[[object], bool]) -> object:
    """``found()`` once it is ``wanted``, or its last value when it never is in time."""
    deadline = time.monotonic() + RESOLVE_WITHIN
    while True:
        value = found()
        if wanted(value) or time.monotonic() > deadline:
            return value
        time.sleep(0.5)


@dataclass
class Container:
    id: str
    state: str
    started: str
    address: str
    ports: str


@dataclass
class Harness:
    """The walkthrough's steps over one machine. Each ``phase_*`` method is one phase."""

    machine: Machine
    walk: Walkthrough = field(default_factory=Walkthrough)

    def __post_init__(self) -> None:
        m = self.machine
        # `a`: built from repos, taken through every verb. `b`: built from a template, runs
        # beside `a`. `o`: an agent and nothing else, turned into an orphan. `c`: never comes up.
        self.a, self.b, self.o = m.set_name("a"), m.set_name("b"), m.set_name("o")
        self.c = m.set_name("c")
        self.from_repos, self.from_set = f"e2e-{RUN_ID}-repos", f"e2e-{RUN_ID}-set"

    # --- running and checking ------------------------------------------------------------

    def sandbox(
        self, *args: str, exits: int = 0, sudo: bool = False, stdin: str | None = None
    ) -> Completed:
        """Run ``sandbox ARGS`` and check its exit status; a wrong one ends the phase, since
        what it would check next is that command's effect."""
        done = self.machine.run(*args, sudo=sudo, stdin=stdin)
        held = self.check(
            done,
            f"exits {exits}",
            lambda: that(done.status == exits, f"exited {done.status}, expected {exits}"),
        )
        if not held:
            raise Stop
        return done

    def check(self, done: Completed, checked: str, holds: Callable[[], object]) -> bool:
        return self.walk.check(done.command, checked, holds, done.output)

    def says(self, done: Completed, checked: str, *fragments: str) -> bool:
        """Check that the command's output has every one of ``fragments``."""
        missing = [fragment for fragment in fragments if fragment not in done.output]
        return self.check(done, checked, lambda: that(not missing, f"not in its output: {missing}"))

    # --- reading the machine -------------------------------------------------------------

    def hostname(self, service: str, set_name: str) -> str:
        return f"{service}.{set_name}.{self.machine.domain}"

    def containers(self, set_name: str) -> dict[str, Container]:
        """The set's containers, running or not, by service, as Docker reports them."""
        listed = self.machine.command(
            ["docker", "ps", "--all", "--quiet", "--filter",
             f"label={PROJECT_LABEL}={project_name(set_name)}"]
        )  # fmt: skip
        that(listed.status == 0, f"`{listed.command}` failed: {listed.output}")
        found: dict[str, Container] = {}
        for container_id in listed.stdout.split():
            shown = self.machine.command(["docker", "inspect", container_id])
            that(shown.status == 0, f"`{shown.command}` failed: {shown.output}")
            data = cast(dict[str, Any], json.loads(shown.stdout)[0])
            networks = cast(dict[str, dict[str, Any]], data["NetworkSettings"]["Networks"])
            service = str(data["Config"]["Labels"]["com.docker.compose.service"])
            found[service] = Container(
                id=container_id,
                state=str(data["State"]["Status"]),
                started=str(data["State"]["StartedAt"]),
                address=next((str(n["IPAddress"]) for n in networks.values()), ""),
                ports=json.dumps(data["HostConfig"]["PortBindings"] or {}),
            )
        return found

    def docker_names(self, kind: str, set_name: str) -> list[str]:
        """The set's networks or volumes that Docker still has."""
        listed = self.machine.command(
            ["docker", kind, "ls", "--quiet", "--filter",
             f"label={PROJECT_LABEL}={project_name(set_name)}"]
        )  # fmt: skip
        that(listed.status == 0, f"`{listed.command}` failed: {listed.output}")
        return listed.stdout.split()

    def left_in_docker(self, set_name: str) -> dict[str, list[str]]:
        left = {
            "containers": sorted(self.containers(set_name)),
            "networks": self.docker_names("network", set_name),
            "volumes": self.docker_names("volume", set_name),
        }
        return {kind: names for kind, names in left.items() if names}

    def git(self, repo: Path, *args: str) -> str:
        done = self.machine.command(["git", "-C", str(repo), *args])
        that(done.status == 0, f"`{done.command}` failed: {done.output}")
        return done.stdout.strip()

    def agent_branch_in(self, repo: str, set_name: str) -> str:
        """``agent/SET`` as the source repo ``repo`` lists it: empty when it has none."""
        return self.git(self.machine.code / repo, "branch", "--list", f"agent/{set_name}")

    def set_dir(self, set_name: str) -> Path:
        return self.machine.sets_root / set_name

    def check_clones(self, done: Completed, set_name: str) -> None:
        def on_the_agent_branch() -> None:
            for repo in REPOS:
                clone = self.set_dir(set_name) / repo
                branch = self.git(clone, "rev-parse", "--abbrev-ref", "HEAD")
                that(branch == f"agent/{set_name}", f"{clone} is on {branch!r}")
                that(self.agent_branch_in(repo, set_name), f"{repo} has no agent/{set_name}")

        self.check(
            done,
            f"each repo has a clone in the set, on agent/{set_name}, and the branch is in the repo",
            on_the_agent_branch,
        )

    def check_running(self, done: Completed, set_name: str) -> None:
        def running() -> None:
            found = self.containers(set_name)
            that(sorted(found) == SERVICES, f"the set's containers: {sorted(found)}")
            states = {service: c.state for service, c in found.items()}
            that(set(states.values()) == {"running"}, f"container states: {states}")

        self.check(
            done,
            "the agent, the web app, toolbox and one shared mysql are running",
            running,
        )

    def check_resolves(self, done: Completed, set_name: str, hosts: list[str]) -> None:
        """Each set hostname, through the operating system, is its container's address."""

        def resolves() -> None:
            found = self.containers(set_name)
            for host in hosts:
                name = self.hostname(host, set_name)
                address = found[EXTRA_HOSTS.get(host, host)].address
                got = eventually(lambda name=name: lookup(name), lambda g, a=address: g == a)
                that(got == address, f"{name} resolves to {got}; its container is at {address}")

        names = ", ".join(self.hostname(host, set_name) for host in hosts)
        self.check(
            done, f"the operating system resolves {names} to the containers' addresses", resolves
        )

    # --- the phases ----------------------------------------------------------------------

    def phase_setup(self) -> None:
        m = self.machine
        refused = self.sandbox("setup", exits=1)
        self.says(
            refused, "without sudo it refuses, naming the command to run", "sudo sandbox setup"
        )

        done = self.sandbox("setup", sudo=True)
        self.says(
            done,
            "says what it installed: the resolver configuration and the mkcert CA",
            f"names under .{m.domain} are looked up at",
            "mkcert: CA installed",
        )
        self.check(
            done,
            f"the machine sends names under .{m.domain}, and no other domain, to the resolver",
            lambda: that(
                installed_domains(Host()) == [m.domain],
                f"domains sent to the resolver: {installed_domains(Host())}",
            ),
        )

        def ca_is_the_users() -> None:
            caroot = m.command(["mkcert", "-CAROOT"])
            ca = Path(caroot.stdout.strip()) / "rootCA.pem"
            that(ca.is_file(), f"{ca} does not exist")
            that(ca.stat().st_uid == os.getuid(), f"{ca} is not the user's")

        self.check(done, "the mkcert CA is the invoking user's, not root's", ca_is_the_users)

    def phase_up_repos(self) -> None:
        m, a = self.machine, self.a
        done = self.sandbox(a, "up", "--repos", *REPOS)
        self.says(
            done,
            "reports the set as provisioned, its plan as created, and its hostnames",
            f"set {a}: {self.set_dir(a)} (provisioned)",
            f"{plan_path(self.set_dir(a))} (created)",
            *(self.hostname(name, a) for name in ("mysql", "toolbox", "web", "admin")),
        )
        self.check_clones(done, a)
        self.check_running(done, a)
        self.check(
            done,
            "the init hook of `jobs` ran in the shared mysql (database web_app exists)",
            lambda: self._has_database(a, "web_app"),
        )

        def publishes_nothing() -> None:
            published = {s: c.ports for s, c in self.containers(a).items() if c.ports != "{}"}
            that(not published, f"published ports: {published}")

        self.check(
            done, "no container of the set publishes a port on the machine", publishes_nothing
        )
        self.check_resolves(done, a, ["web", "admin", "mysql", "agent"])

        expected = {
            "user": 65534,
            "WORKTREE": str(self.set_dir(a) / "web"),
            "APP_URL": f"https://{self.hostname('web', a)}",
        }
        for name in ("web", "admin"):
            url = f"https://{self.hostname(name, a)}/"
            # No --resolve, no --cacert, no -k: the name comes from the operating system and
            # trust from the CA setup installed. --noproxy keeps a proxy away from the names.
            got = m.command(["curl", "-sS", "--fail", "--noproxy", "*", url])
            self.walk.check(
                got.command,
                "the web app answers HTTPS at its set hostname, trusted through the installed CA",
                lambda got=got: that(
                    got.status == 0 and json.loads(got.stdout) == expected,
                    f"exit {got.status}, expected the body {expected}",
                ),
                got.output,
            )

    def _has_database(self, set_name: str, database: str) -> None:
        mysql = self.containers(set_name)["mysql"].id
        shown = self.machine.command(
            ["docker", "exec", mysql, "mysql", "-uroot", "-psandbox", "-N", "-e", "SHOW DATABASES"]
        )
        that(database in shown.stdout.split(), f"databases: {shown.output}")

    def phase_save_repos(self) -> None:
        m = self.machine
        refused = self.sandbox("save", "--to", self.from_repos, exits=2)
        self.says(refused, "without a set or --repos it is a usage error", "save needs a source")

        done = self.sandbox("save", "--to", self.from_repos, "--repos", *REPOS)
        path = m.templates_root / f"{self.from_repos}.yaml"
        self.says(done, "names the template file it wrote", f"template {self.from_repos}: {path}")

        def pins_sources_only() -> None:
            repos = yaml.safe_load(path.read_text())["repos"]
            wanted = {repo: {"source": str(m.code / repo)} for repo in REPOS}
            that(repos == wanted, f"{path} has repos {repos}, expected {wanted}")

        self.check(
            done,
            "the template pins each repo's absolute source and no base branch",
            pins_sources_only,
        )

    def phase_save_set(self) -> None:
        m = self.machine
        done = self.sandbox(self.a, "save", "--to", self.from_set)
        path = m.templates_root / f"{self.from_set}.yaml"
        self.says(done, "names the template file it wrote", f"template {self.from_set}: {path}")

        def is_the_sets_plan() -> None:
            template = yaml.safe_load(path.read_text())
            plan = yaml.safe_load(plan_path(self.set_dir(self.a)).read_text())
            that(template == plan, f"{path} is {template}; the set's plan is {plan}")
            bases = {name: repo.get("base") for name, repo in template["repos"].items()}
            that(bases == dict.fromkeys(REPOS, "main"), f"bases in the template: {bases}")

        self.check(done, "the template is the set's plan, base branches included", is_the_sets_plan)

    def phase_templates(self) -> None:
        m = self.machine
        done = self.sandbox("templates")

        def lists_what_is_there() -> None:
            on_disk = sorted(path.stem for path in m.templates_root.glob("*.yaml"))
            that(self.from_repos in on_disk, f"{self.from_repos} is not in {m.templates_root}")
            listed = done.stdout.split()
            that(listed == on_disk, f"listed {listed}; the templates root has {on_disk}")

        self.check(
            done, "lists exactly the saved templates, one name per line", lists_what_is_there
        )
        refused = self.sandbox(self.a, "templates", exits=2)
        self.says(refused, "with a set it is a usage error", "templates takes no set")

    def phase_up_from(self) -> None:
        b = self.b
        done = self.sandbox(b, "up", "--from", self.from_repos)
        self.says(
            done, "reports the set as provisioned", f"set {b}: {self.set_dir(b)} (provisioned)"
        )
        self.check_clones(done, b)
        self.check_running(done, b)

        def pinned() -> None:
            plan = yaml.safe_load(plan_path(self.set_dir(b)).read_text())
            wanted = {
                repo: {"source": str(self.machine.code / repo), "base": "main"} for repo in REPOS
            }
            that(plan["repos"] == wanted, f"the set's plan has {plan['repos']}, expected {wanted}")

        self.check(
            done,
            "the set's plan has the template's sources, with the checked-out branch as base",
            pinned,
        )
        self.check_resolves(done, b, ["mysql", "web"])

    def phase_two_sets(self) -> None:
        """Both sets run the same mysql on its conventional port, each at its own address."""
        addresses: dict[str, str] = {}
        for name in (self.a, self.b):
            host = self.hostname("mysql", name)
            label = f"connect to {host}:3306 (name from the operating system resolver)"

            def greets(name: str = name, host: str = host) -> None:
                address = eventually(lambda: lookup(host), lambda got: got is not None)
                that(isinstance(address, str), f"{host} does not resolve")
                with socket.create_connection((host, 3306), timeout=10) as connection:
                    connection.settimeout(10)
                    greeting = connection.recv(128)
                that(b"8.4" in greeting, f"no MySQL 8.4 greeting from {host}:3306: {greeting!r}")
                addresses[name] = str(address)

            self.walk.check(label, "MySQL answers there, with nothing published", greets)
        self.walk.check(
            f"connect to mysql.<set>.{self.machine.domain}:3306 in both sets",
            "the two sets' mysql are different containers at different addresses",
            lambda: that(
                len(addresses) == 2 and len(set(addresses.values())) == 2,
                f"addresses by set: {addresses}",
            ),
        )

    def phase_repeated_up(self) -> None:
        a = self.a
        before = {service: c.id for service, c in self.containers(a).items()}
        done = self.sandbox(a, "up")
        self.says(
            done,
            "reports the set as already running",
            f"set {a}: {self.set_dir(a)} (already running)",
        )

        def untouched() -> None:
            after = {service: c.id for service, c in self.containers(a).items()}
            that(after == before, f"containers before {before}, after {after}")

        self.check(done, "the running containers are left as they were", untouched)

    def phase_recreate(self) -> None:
        a = self.a
        before = self.containers(a)
        done = self.sandbox(a, "up", "--recreate")
        self.says(done, "reports the set as recreated", f"set {a}: {self.set_dir(a)} (recreated)")

        def restarted() -> None:
            after = self.containers(a)
            same = [s for s in SERVICES if s in before and after[s].started <= before[s].started]
            that(not same, f"not restarted: {same}")

        self.check(done, "every service of the set was restarted", restarted)
        self.check_running(done, a)
        self.check(
            done,
            "the init hook ran again in the new mysql (database web_app exists)",
            lambda: self._has_database(a, "web_app"),
        )
        self.check_resolves(done, a, ["web", "admin", "mysql", "agent"])

    def phase_status(self) -> None:
        a, domain = self.a, self.machine.domain
        listing = self.sandbox("status")

        def row() -> None:
            rows = [line.split() for line in listing.stdout.splitlines()]
            that([a, "running"] in [r[:2] for r in rows], f"no `{a}  running` row")

        self.check(listing, "the table has a row for the set, as running", row)

        listing_json = self.sandbox("status", "--json")

        def entry() -> None:
            sets = {s["name"]: s["state"] for s in json.loads(listing_json.stdout)["sets"]}
            that(sets.get(a) == "running", f"sets and states: {sets}")

        self.check(listing_json, "prints JSON that lists the set as running", entry)

        detail = self.sandbox(a, "status")
        self.says(
            detail,
            "shows the set as running, with each service's set hostname and each repo's branch",
            f"set {a}: running [{project_name(a)}]",
            *(self.hostname(service, a) for service in SERVICES),
            f"agent/{a}",
        )

        detail_json = self.sandbox(a, "status", "--json")

        def facts() -> None:
            shown = json.loads(detail_json.stdout)
            that(shown["state"] == "running", f"state: {shown['state']}")
            containers = self.containers(a)
            got = {s["name"]: (s["state"], s["hostname"], s["ip"]) for s in shown["services"]}
            wanted = {
                service: ("running", f"{service}.{a}.{domain}", containers[service].address)
                for service in SERVICES
            }
            that(got == wanted, f"services {got}, expected {wanted}")
            repos = {r["name"]: (r["exists"], r["branch"], r["modified"]) for r in shown["repos"]}
            that(
                repos == dict.fromkeys(REPOS, (True, f"agent/{a}", False)),
                f"repos (exists, branch, modified): {repos}",
            )

        self.check(
            detail_json,
            "prints JSON with every service running at its set hostname and its container's "
            "address, and every repo's clone on the agent branch",
            facts,
        )
        missing = self.sandbox(f"{a}-nope", "status", exits=1)
        self.says(missing, "a set that does not exist is an error naming it", f"{a}-nope")

    def phase_urls(self) -> None:
        a = self.a
        table = self.sandbox(a, "urls")
        as_json = self.sandbox(a, "urls", "--json")
        found: dict[str, Any] = {}

        def addresses() -> None:
            found.update(json.loads(as_json.stdout))
            that(found["running"] is True, f"running: {found['running']}")
            web = {w["url"] for w in found["web"]}
            wanted = {f"https://{self.hostname(name, a)}" for name in ("web", "admin")}
            that(web == wanted, f"web apps {web}, expected {wanted}")
            services = {s["address"] for s in found["services"]}
            wanted = {f"{self.hostname('mysql', a)}:3306", self.hostname("toolbox", a)}
            that(services == wanted, f"services {services}, expected {wanted}")
            that(found["agent"]["address"] == self.hostname("agent", a), f"agent: {found['agent']}")

        self.check(
            as_json,
            "prints JSON with the web app's two URLs, mysql on 3306, toolbox without a port, "
            "and the agent's hostname",
            addresses,
        )

        def current_addresses() -> None:
            containers = self.containers(a)
            listed = [*found["web"], *found["services"], found["agent"]]
            wrong = {
                entry.get("url") or entry["address"]: entry["ip"]
                for entry in listed
                if entry["ip"] != containers[entry["service"]].address
            }
            that(listed and not wrong, f"addresses that are not the container's: {wrong}")

        self.check(
            as_json, "each address comes with its container's current address", current_addresses
        )

        def same_facts() -> None:
            listed = [*found["web"], *found["services"], found["agent"]]
            that(listed, "nothing to compare: the JSON was not read")
            lines = [line.split() for line in table.stdout.splitlines()]
            for entry in listed:
                row = [entry.get("url") or entry["address"], entry["ip"]]
                that(row in lines, f"no row {row} in the table")

        self.check(table, "the table shows the same addresses as the JSON", same_facts)

    def phase_sh(self) -> None:
        a = self.a
        workspace = f"/workspace/{a}"
        agent = self.sandbox(a, "sh", stdin="pwd; hostname -i\n")
        self.check(
            agent,
            "opens a shell in the agent, in the set's workspace",
            lambda: that(
                agent.stdout.split() == [workspace, self.containers(a)["agent"].address],
                f"pwd and address printed: {agent.stdout.split()}",
            ),
        )
        # The shell's own exit status is the verb's.
        self.sandbox(a, "sh", stdin="exit 7\n", exits=7)

        web = f"https://{self.hostname('web', a)}/"
        from_agent = self.sandbox(a, "sh", stdin=f"curl -sS --fail {web}\n")
        self.check(
            from_agent,
            "from that shell the web app answers HTTPS at the same set hostname as outside",
            lambda: that(
                json.loads(from_agent.stdout)["APP_URL"] == web.rstrip("/"),
                f"body: {from_agent.stdout!r}",
            ),
        )

        repo = self.sandbox(
            a, "sh", "--repo", "api", stdin="pwd; git rev-parse --abbrev-ref HEAD\n"
        )
        self.check(
            repo,
            "--repo starts in the agent's clone of that repo, on the agent branch",
            lambda: that(
                repo.stdout.split() == [f"{workspace}/api", f"agent/{a}"],
                f"pwd and branch printed: {repo.stdout.split()}",
            ),
        )
        # The mysql image has no `hostname`; a container's /etc/hostname is its short ID.
        service = self.sandbox(
            a, "sh", "mysql", stdin='echo "$MYSQL_DATABASE"; cat /etc/hostname\n'
        )
        self.check(
            service,
            "SERVICE opens the shell in that service's container",
            lambda: that(
                service.stdout.split() == ["app", self.containers(a)["mysql"].id],
                f"database and container printed: {service.stdout.split()}",
            ),
        )
        unknown = self.sandbox(a, "sh", "nope", exits=2)
        self.says(
            unknown,
            "an unknown service is a usage error listing the set's services",
            "no service 'nope'",
            ", ".join(SERVICES),
        )
        unknown_repo = self.sandbox(a, "sh", "--repo", "nope", exits=2)
        self.says(
            unknown_repo,
            "an unknown repo is a usage error listing the set's repos",
            "no repo 'nope'",
        )
        both = self.sandbox(a, "sh", "mysql", "--repo", "api", exits=2)
        self.says(
            both, "SERVICE with --repo is a usage error", "--repo applies to the agent's shell"
        )

    def phase_claude(self) -> None:
        """Claude Code as far as it goes without a login: it starts in the agent and answers."""
        a = self.a
        done = self.sandbox(a, "claude", "--version")
        self.check(
            done,
            "starts Claude Code in the agent, passing --version through: it prints its version",
            lambda: that(CLAUDE_VERSION.search(done.stdout), f"stdout: {done.stdout!r}"),
        )
        in_agent = self.machine.command(
            ["docker", "exec", self.containers(a)["agent"].id, "claude", "--version"]
        )
        self.check(
            done,
            "that is the version of the Claude Code in the set's agent container",
            lambda: that(
                in_agent.status == 0 and in_agent.stdout.strip() in done.stdout,
                f"in the container: {in_agent.output!r}; from the verb: {done.stdout!r}",
            ),
        )

    def phase_prune_orphans(self) -> None:
        """Bare ``prune``: an agent left running without its plan goes; other sets stay."""
        o, a = self.o, self.a
        up = self.sandbox(o, "up", "--repos", "docs")
        self.says(
            up, "a set of a repo without services is an agent and nothing else", "services: none"
        )
        plan_path(self.set_dir(o)).unlink()

        listing = self.sandbox("status", "--json")

        def orphaned() -> None:
            sets = {s["name"]: s["state"] for s in json.loads(listing.stdout)["sets"]}
            that(sets.get(o) == "orphaned", f"sets and states: {sets}")

        self.check(
            listing, "a set whose plan is gone but whose containers run is orphaned", orphaned
        )

        others = {service: c.id for service, c in self.containers(a).items()}
        done = self.sandbox("prune")
        self.says(done, "names the orphaned set it pruned", f"set {o}: pruned {project_name(o)}")
        self.check(
            done,
            "the orphan's containers, network and volumes are gone",
            lambda: that(not self.left_in_docker(o), f"left in Docker: {self.left_in_docker(o)}"),
        )
        self.check(
            done,
            "the orphan's clone is kept",
            lambda: that((self.set_dir(o) / "docs" / "README.md").is_file(), "the clone is gone"),
        )

        def untouched() -> None:
            now = {service: c.id for service, c in self.containers(a).items()}
            that(now == others, f"containers of {a} before {others}, after {now}")

        self.check(done, "a set with a plan is left alone", untouched)

    def phase_down(self) -> None:
        a = self.a
        commit = self.sandbox(
            a, "sh", "--repo", "api",
            stdin="echo work > agent.txt && git add agent.txt && git -c user.name=agent "
            "-c user.email=agent@example.invalid commit -q -m 'agent work'\n",
        )  # fmt: skip
        self.check(
            commit,
            "the agent's commit stays in its clone until the set is synced",
            lambda: that(
                self.git(self.machine.code / "api", "log", "-1", "--format=%s", f"agent/{a}")
                == "initial",
                "agent/SET in the repo already moved",
            ),
        )

        done = self.sandbox(a, "down")
        self.says(
            done,
            "reports the set stopped and its clones kept",
            f"{project_name(a)} stopped; clones kept in {self.set_dir(a)}",
        )
        self.check(
            done,
            "the set's containers, network and volumes are gone",
            lambda: that(not self.left_in_docker(a), f"left in Docker: {self.left_in_docker(a)}"),
        )
        self.check(
            done,
            "the agent's clones are kept",
            lambda: that(
                all((self.set_dir(a) / repo / ".git").exists() for repo in REPOS), "a clone is gone"
            ),
        )
        self.check(
            done,
            "the agent's commit is on agent/SET in the repo",
            lambda: that(
                self.git(self.machine.code / "api", "log", "-1", "--format=%s", f"agent/{a}")
                == "agent work",
                "agent/SET in the repo does not have the agent's commit",
            ),
        )

        def gone() -> None:
            for service in ("web", "mysql", "agent"):
                name = self.hostname(service, a)
                got = eventually(lambda name=name: lookup(name), lambda g: g is None)
                that(got is None, f"{name} still resolves to {got}")

        self.check(done, "the set's hostnames no longer resolve through the operating system", gone)

        status = self.sandbox(a, "status", "--json")

        def stopped() -> None:
            shown = json.loads(status.stdout)
            that(shown["state"] == "stopped", f"state: {shown['state']}")
            with_address = [s["name"] for s in shown["services"] if s["ip"] is not None]
            that(not with_address, f"services with an address: {with_address}")

        self.check(status, "the set is stopped, and no service has an address", stopped)

        urls = self.sandbox(a, "urls", "--json")

        def not_running() -> None:
            shown = json.loads(urls.stdout)
            that(shown["running"] is False, f"running: {shown['running']}")
            listed = [*shown["web"], *shown["services"], shown["agent"]]
            that(len(listed) == 5, f"addresses listed: {listed}")
            that(all(entry["ip"] is None for entry in listed), f"addresses: {listed}")

        self.check(
            urls,
            "the same five addresses are listed, as not running and without container addresses",
            not_running,
        )
        claude = self.sandbox(a, "claude", "--version", exits=1)
        self.says(
            claude,
            "claude on a stopped set says so and how to start it",
            "is not running",
            f"sandbox {a} up",
        )

    def phase_destroy(self) -> None:
        """``destroy`` of the stopped set `a`: its refusals, then ``--force``."""
        a = self.a
        set_dir = self.set_dir(a)

        def nothing_removed() -> None:
            that(plan_path(set_dir).is_file(), "the plan is gone")
            that(all((set_dir / repo / ".git").exists() for repo in REPOS), "a clone is gone")
            that(all(self.agent_branch_in(repo, a) for repo in REPOS), "an agent branch is gone")

        unpushed = self.sandbox(a, "destroy", "--branches", exits=1)
        self.says(
            unpushed,
            "--branches refuses to delete a branch holding the agent's unpushed commit",
            f"--branches would delete agent/{a}",
            "agent work",
        )
        self.check(unpushed, "the refusal removed nothing", nothing_removed)

        (set_dir / "web" / "notes.txt").write_text("not committed\n")
        dirty = self.sandbox(a, "destroy", exits=1)
        self.says(
            dirty,
            "a clone with untracked work is refused, naming the file and --force",
            "work that destroy would discard",
            "notes.txt",
            "--force",
        )
        self.check(dirty, "the refusal removed nothing", nothing_removed)

        done = self.sandbox(a, "destroy", "--force")
        self.says(
            done,
            "--force destroys the set, naming the clones removed and the branches kept",
            f"set {a}: destroyed {set_dir}",
            f"removed clone {set_dir / 'web'}",
            f"kept branch agent/{a} in {self.machine.code / 'api'}",
        )
        self.check(
            done,
            "the set directory is gone",
            lambda: that(not set_dir.exists(), f"{set_dir} exists"),
        )
        self.check(
            done,
            "without --branches the agent branches stay, the agent's commit with them",
            lambda: that(
                all(self.agent_branch_in(repo, a) for repo in REPOS)
                and self.git(self.machine.code / "api", "log", "-1", "--format=%s", f"agent/{a}")
                == "agent work",
                "an agent branch, or the commit on it, is gone",
            ),
        )
        listing = self.sandbox("status", "--json")
        self.check(
            listing,
            "status no longer lists the set",
            lambda: that(
                a not in [s["name"] for s in json.loads(listing.stdout)["sets"]], "still listed"
            ),
        )
        again = self.sandbox(a, "destroy", exits=1)
        self.says(again, "destroying it again is an error naming the missing set", f"no set '{a}'")

    def phase_prune_set(self) -> None:
        """``SET prune`` and ``destroy`` refuse a running set; a stopped one is cleared."""
        b = self.b
        for verb in ("prune", "destroy"):
            refused = self.sandbox(b, verb, exits=1)
            self.says(
                refused,
                f"{verb} refuses a running set, naming `down`",
                "is running",
                f"sandbox {b} down",
            )
            self.check_running(refused, b)

        # Stopped behind sandbox's back (as a reboot leaves a set): containers, network and
        # volumes are all still there, which is what `SET prune` is for.
        ids = [container.id for container in self.containers(b).values()]
        stopped = self.machine.command(["docker", "stop", "-t", "5", *ids])
        that(stopped.status == 0, f"`{stopped.command}` failed: {stopped.output}")

        status = self.sandbox(b, "status", "--json")
        self.check(
            status,
            "a set whose containers all stopped is reported as stopped",
            lambda: that(json.loads(status.stdout)["state"] == "stopped", status.stdout),
        )
        that(self.left_in_docker(b), "the stopped set left nothing in Docker to prune")

        done = self.sandbox(b, "prune")
        self.says(
            done,
            "reports the project pruned and the plan and clones kept",
            f"set {b}: pruned {project_name(b)}",
            "plan and clones kept",
        )
        self.check(
            done,
            "the set's containers, network and volumes are gone",
            lambda: that(not self.left_in_docker(b), f"left in Docker: {self.left_in_docker(b)}"),
        )
        self.check(
            done,
            "the plan and the clones are kept",
            lambda: that(
                plan_path(self.set_dir(b)).is_file()
                and all((self.set_dir(b) / repo / ".git").exists() for repo in REPOS),
                "the plan or a clone is gone",
            ),
        )

        def resolver_runs() -> None:
            shown = self.machine.command(
                ["docker", "inspect", "--format", "{{.State.Status}}", RESOLVER_CONTAINER]
            )
            that(shown.stdout.strip() == "running", f"the resolver: {shown.output.strip()}")

        self.check(done, "the resolver keeps running for other sets", resolver_runs)

    def phase_destroy_branches(self) -> None:
        """``destroy --branches`` of the pruned set `b`, which has no work of the agent's."""
        b = self.b
        done = self.sandbox(b, "destroy", "--branches")
        self.says(
            done,
            "destroys the set, naming the branches deleted",
            f"set {b}: destroyed {self.set_dir(b)}",
            *(f"deleted branch agent/{b} in {self.machine.code / repo}" for repo in REPOS),
        )
        self.check(
            done,
            "the set directory is gone",
            lambda: that(not self.set_dir(b).exists(), f"{self.set_dir(b)} exists"),
        )
        self.check(
            done,
            "agent/SET is gone from every repo, and each repo is still on main",
            lambda: that(
                not any(self.agent_branch_in(repo, b) for repo in REPOS)
                and all(
                    self.git(self.machine.code / repo, "rev-parse", "--abbrev-ref", "HEAD")
                    == "main"
                    for repo in REPOS
                ),
                "an agent branch is left, or a repo moved off main",
            ),
        )

    def phase_uninstall(self) -> None:
        m, c = self.machine, self.c
        done = self.sandbox("setup", "--uninstall", sudo=True)
        self.says(
            done,
            "says what it removed, and how to remove the mkcert CA it leaves",
            "removed the resolver",
            "mkcert -uninstall",
        )
        self.check(
            done,
            "the machine no longer sends any domain to the resolver",
            lambda: that(not installed_domains(Host()), f"still: {installed_domains(Host())}"),
        )

        def resolver_gone() -> None:
            listed = m.command(
                ["docker", "ps", "--all", "--quiet", "--filter", f"name=^{RESOLVER_CONTAINER}$"]
            )
            that(listed.status == 0 and not listed.stdout.split(), f"still there: {listed.output}")

        self.check(done, "the resolver's container is gone", resolver_gone)

        def ca_kept() -> None:
            ca = Path(m.command(["mkcert", "-CAROOT"]).stdout.strip()) / "rootCA.pem"
            that(ca.is_file(), f"{ca} is gone")

        self.check(done, "the mkcert CA is left installed", ca_kept)

        refused = self.sandbox(c, "up", "--repos", *REPOS, exits=1)
        self.says(
            refused,
            "`up` now refuses, naming the setup command",
            "this machine cannot reach the set",
            "sudo sandbox setup",
        )
        self.check(
            refused,
            "the refused `up` started nothing",
            lambda: that(not self.left_in_docker(c), f"left in Docker: {self.left_in_docker(c)}"),
        )

    # --- teardown ------------------------------------------------------------------------

    def teardown(self) -> None:
        """Remove whatever of this run is left: through ``sandbox`` where a set still has
        its plan, then directly in Docker, then the setup unless it is being kept."""
        m = self.machine
        for name in m.created_sets:
            if plan_path(self.set_dir(name)).is_file():
                # Each is checked on its own: a failed `down` must not skip the `destroy`.
                for args in (("down",), ("destroy", "--force", "--branches")):
                    done = m.run(name, *args)
                    self.check(
                        done,
                        "teardown: exits 0",
                        lambda done=done: that(done.status == 0, f"exited {done.status}"),
                    )
            # What sandbox could not be asked to remove (an orphan, a half-made set).
            project = project_name(name)
            m.command(
                ["docker", "network", "disconnect", "--force", f"{project}_default",
                 RESOLVER_CONTAINER]
            )  # fmt: skip
            m.command(["docker", "compose", "-p", project, "down", "--volumes", "--remove-orphans"])

        def nothing_left() -> None:
            left = {name: self.left_in_docker(name) for name in m.created_sets}
            that(not any(left.values()), f"left in Docker: {left}")

        self.walk.check(
            "docker ps / network ls / volume ls, for every set of this run",
            "teardown: no container, network or volume of this run is left",
            nothing_left,
        )
        if not m.keep_setup:
            done = m.run("setup", "--uninstall", sudo=True)
            self.check(
                done,
                "teardown: exits 0",
                lambda: that(done.status == 0, f"exited {done.status}"),
            )

    def phases(self) -> list[Phase]:
        """Every phase, in order, with the phases it would be meaningless without."""
        return [
            Phase("setup", self.phase_setup),
            Phase("up --repos", self.phase_up_repos, needs=("setup",)),
            Phase("save --repos", self.phase_save_repos),
            Phase("SET save", self.phase_save_set, needs=("up --repos",)),
            Phase("templates", self.phase_templates, needs=("save --repos",)),
            Phase("up --from", self.phase_up_from, needs=("setup", "save --repos")),
            Phase("two sets", self.phase_two_sets, needs=("up --repos", "up --from")),
            Phase("repeated up", self.phase_repeated_up, needs=("up --repos",)),
            Phase("up --recreate", self.phase_recreate, needs=("up --repos",)),
            Phase("status", self.phase_status, needs=("up --repos",)),
            Phase("urls", self.phase_urls, needs=("up --repos",)),
            Phase("sh", self.phase_sh, needs=("up --repos",)),
            Phase("claude", self.phase_claude, needs=("up --repos",)),
            Phase("prune", self.phase_prune_orphans, needs=("setup",)),
            Phase("down", self.phase_down, needs=("up --repos",)),
            Phase("destroy", self.phase_destroy, needs=("down",)),
            Phase("SET prune", self.phase_prune_set, needs=("up --from",)),
            Phase("destroy --branches", self.phase_destroy_branches, needs=("SET prune",)),
            Phase("setup --uninstall", self.phase_uninstall, needs=("setup",)),
        ]


# Not run when an existing setup is kept: it is there already, and it stays.
SETUP_PHASES = ("setup", "setup --uninstall")


def test_every_verb(machine: Machine, request: pytest.FixtureRequest) -> None:
    harness = Harness(machine)
    given = SETUP_PHASES if machine.keep_setup else ()
    try:
        report = harness.walk.run(harness.phases(), harness.teardown, given=given)
    finally:
        # Also when interrupted: what ran so far is what the developer needs to see.
        request.config.stash[REPORT_KEY] = harness.walk.report.render()
    if not report.ok:
        pytest.fail(f"walkthrough: {report.summary()} (report below)", pytrace=False)
