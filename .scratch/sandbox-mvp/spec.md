# Spec: `sandbox` — multi-repo agent environment provisioner (MVP)

Source: "Multi-repo agent environment provisioner: concept" (claude.ai artifact C9wKnYAVNbtWeAbxCjbbJc), all open questions resolved.
Labels (when published): `ready-for-agent`

## Problem Statement

I work across several repositories that together make up one product (e.g. an `api` and a `web` repo that both need MySQL and Redis). I want to hand a unit of work, such as ticket `jira-123`, to a coding agent, and I want to run several of these agents at once.

Today that is painful:

- Each agent needs its own copy of every repo involved, on its own branch, without trampling my checkout or another agent's.
- The repos need backing services. Running one stack per agent collides on ports (3306, 6379, 443), and remapping ports means templating every app's env file, which drifts from the committed config.
- Two repos that both declare `mysql` should share one instance inside an environment, but nothing tells me when their definitions silently disagree.
- Web apps need real HTTPS hostnames so cookies, CORS, OAuth callbacks and Vite HMR behave like they do in the committed config.
- The agent needs to run inside a container with sensible hardening, my Claude login, and access to its worktrees, but never the Docker socket.
- When something crashes halfway, I'm left with orphaned containers, stale `/etc/hosts` lines and half-created worktrees that I clean up by hand.

## Solution

A Python CLI, `sandbox`, that provisions isolated **sets**. A set is named after a unit of work (e.g. `jira-123`) and contains:

- the agent's own clone of each repo, on branch `agent/<set>`, synced back into the repo as `agent/<set>`
- one deduplicated instance of each backing service those repos need
- an agent container with the clones mounted

> **Revision (agent clones).** The MVP first gave each set a `git worktree` of every repo, with the repo's `.git` mounted into the agent. Any part of a shared `.git` the agent can write and host git later reads or writes is a route onto the host: config and hooks (code exec), and symlinks under `logs/`, `refs/` or `objects/` that host git then writes through (agent-chosen bytes into a file of the agent's choosing). A read-only bind of `.git/config` alone is detached when host git renames a new config over it, and narrow read-write binds of `.git` subdirectories still allow the symlink swap. So the set's repos are now **the agent's own clones** (`git clone --shared`: the source's objects borrowed read-only through `alternates`), the source's `.git` is mounted **read-only as a whole**, and sandbox **syncs** the clone's `agent/<set>` into the source repo on `up`, `SET status`, `down` and `destroy`, using host git with isolated config (pinned program-running keys, no system/global config, no `GIT_*` env) that never reads the clone's config or hooks and never follows its symlinks (`git upload-pack --strict` on the clone; status from a snapshot git dir). `agent/<set>` in the source repo is therefore not live, and the set is no longer a git worktree. Stories below that say "worktree" now mean the agent's clone; stories 29, 30, 78, 100, 101 and the WorktreeManager module read as revised here.

Each set is a Docker Compose project. Every hostname in a set gets its **own loopback IP** and keeps its **native port**, so `mysql.jira-123.test:3306` and `mysql.jira-456.test:3306` coexist. Inside the set, apps reach services by their short alias (`mysql`), exactly as their committed `.env.example` says. Web apps are served on HTTPS at `https://<app>.<set>.test` with a per-set mkcert wildcard certificate trusted both on the host and inside the containers. The only injected environment is `WORKTREE` and values derived from it (`APP_URL`).

The user runs `sandbox jira-123 up --repos api web`, waits for healthchecks and init hooks, then `sandbox jira-123 claude` to start the agent. `down`, `prune` and `destroy` tear things back down at increasing depth, with safety checks that protect uncommitted and unpushed work. `up` is idempotent and resumes from the set directory after a crash.

## User Stories

### Setup and prerequisites

1. As a developer, I want a one-time `sudo sandbox setup`, so that everything needing root is installed once and the everyday tool stays unprivileged.
2. As a developer, I want setup to install a small root-owned `sandbox-hosts` helper with a NOPASSWD sudoers rule scoped to that binary only, so that `sandbox` can update `/etc/hosts` without a password prompt and without broad sudo rights.
3. As a macOS developer, I want setup to install a launchd job that creates a pool of `lo0` loopback aliases at boot, so that sets can bind to IPs other than 127.0.0.1.
4. As a macOS developer, I want to size that alias pool with `--sets N` (default 16 /24 blocks), so that I can run as many concurrent sets as I need.
5. As a developer, I want setup to run `mkcert -install` as me (from `SUDO_USER`), never as root, so that the CA lands in my normal CAROOT and my browser trusts it.
6. As a developer, I want setup to refuse if the configured IP range is already in use, so that sandbox never hijacks addresses something else relies on.
7. As a developer, I want `sudo sandbox setup --uninstall` to remove the hosts helper, sudoers rule and launchd job, so that I can cleanly remove sandbox.
8. As a developer, I want `--uninstall` to leave the mkcert CA installed and print the `mkcert -uninstall` command, so that I don't lose a CA other tools may use but know how to remove it.
9. As a developer, I want `up` to fail early and name the missing piece if setup hasn't been run (helper missing, alias pool missing, `rootCA.pem` missing), so that I know exactly what to fix.
10. As a Linux developer, I want no loopback setup at all, since the whole 127.0.0.0/8 range already works.
11. As a Windows developer, I want a clear "unsupported platform" error, so that I don't hit confusing locking failures.

### Configuration and plans

12. As a developer, I want a global config at `~/.config/sandbox/config.yaml`, so that I can change the IP pool, the templates root, the sets root and the default agent.
13. As a developer, I want the default IP pool to be 127.42.0.0/16, so that it avoids Debian's 127.0.1.1 and systemd-resolved's 127.0.0.53.
14. As a developer, I want each set to get its own directory `~/sandbox/sets/<set>/` with a plan file `sandbox.yaml` at its root, so that the set's definition lives beside its worktrees.
15. As a developer, I want the set's name to be its directory name, never repeated in the plan, so that there is one source of truth.
16. As a developer, I want the plan to list `repos.<name>.source` (absolute path) and `repos.<name>.base` (the branch the worktree is cut from), so that a set can be recreated from its plan alone.
17. As a developer, I want `base` pinned in a set's plan but optional in a template, so that templates stay reusable while sets stay reproducible.
18. As a developer, I want templates in `~/.config/sandbox/plans/` sharing the plan schema, so that I don't retype large repo lists.
19. As a developer, I want `sandbox SET save --to TEMPLATE` to save an existing set's plan as a template, so that I can reuse a working combination.
20. As a developer, I want `sandbox save --to TEMPLATE --repos NAME...` to build a template from repos in the current directory, so that I can create templates without first creating a set.
21. As a developer, I want `sandbox templates` to list my templates, so that I can find the one I need.
22. As a developer, I want `up` never to overwrite an existing set's plan, so that my edits to it are never lost; to change a set I edit its plan file.
23. As a developer, I want `up` to refuse when I pass a different plan than the one on disk, so that I can't accidentally reshape a running set.
24. As a developer, I want plan and config files validated with clear, field-level errors, so that typos fail fast.

### Creating a set

25. As a developer, I want `sandbox jira-123 up --repos api web` to look for `./api` and `./web` in the current directory, so that I don't need a repo registry.
26. As a developer, I want `sandbox jira-123 up --from TEMPLATE` to copy a template into the new set, so that I can provision a known combination in one command.
27. As a developer, I want a repo to opt in by having a `.sandbox/` directory at its root, with `compose.yaml` and init scripts inside both optional, so that repos without services can still take part.
28. As a developer, I want naming a repo without `.sandbox/` to fail before anything is created, whether it comes from `--repos` or a template, so that I never get a half-built set.
29. As a developer, I want each repo cloned for the agent on branch `agent/<set>` from its `base`, and that branch synced into my repo on `up`, `SET status`, `down` and `destroy` (fast-forward, or following the agent's rewrites when I haven't touched it; never overwriting my own commits on it or a checked-out branch), so that the agent's commits are isolated and easy to find.
30. As a developer, I want clone creation and branch syncing serialised per repo, so that parallel sets on the same repo don't corrupt its `.git`.
31. As a developer, I want a later `up` with the same set name to reuse surviving `agent/<set>` branches and warn that `base` was ignored, so that the agent's earlier work is picked up rather than lost.
32. As a developer, I want all validation (plan, repos, dedup, agent image requirements) to happen before any container starts, so that failures are cheap.
33. As a developer, I want `up` to block until every service's healthcheck passes, so that the set is actually usable when the command returns.
34. As a developer, I want `up` to print the set's bindings on success, so that I immediately know where everything is.
35. Verb names are reserved, so as a developer I want set names that collide with a verb rejected with a clear error.
36. As a developer, I want each `up` to provision exactly one set, and to run several sets by running the command several times in parallel, so that concurrent processes are safe.

### Dependencies and deduplication

37. As a repo maintainer, I want to declare backing services in `.sandbox/compose.yaml` using ordinary Compose, so that I don't learn a new format.
38. As a developer, I want services with the same name across repos to become one instance in the set, so that `api` and `web` share one `mysql`.
39. As a developer, I want each repo's compose file normalised on its own with `docker compose config` before comparison, so that `extends` and similar features don't cause false conflicts.
40. As a developer, I want same-named services with different definitions to fail provisioning with a diff naming both repos, so that I find the incompatibility instead of Compose silently letting the last file win.
41. As a developer, I want `depends_on`, `labels` and `ports` ignored in the comparison, since sandbox rewrites ports.
42. As a developer, I want a healthcheck kept when only one repo sets it, and a conflict when two repos set different ones, so that readiness is never silently weakened.
43. As a developer, I want the `x-sandbox` extension field excluded from the comparison, so that web-app metadata doesn't cause conflicts.
44. As a developer, I want the original repo compose files passed to Compose alongside the override, so that relative paths still resolve from each repo.
45. As a developer, I want to know that multi-version services (e.g. `mysql84` next to `mysql`) are not supported in the MVP, so that I expect conflicting repos to fail.

### Init hooks

46. As a repo maintainer, I want to place `.sandbox/init/<service>.sh` in my repo, so that I can create my own database, users or seed data inside a shared service.
47. As a developer, I want sandbox to mount each hook read-only at `/sandbox/init/<repo>/` in the service and run it with `docker compose exec` once the service is healthy, so that hooks run regardless of the image's own init conventions.
48. As a developer, I want hooks run in repo-name order, so that behaviour is deterministic.
49. As a repo maintainer, I want hooks run on every `up`, including a resume, so that I can rely on them for convergence (and I know they must be idempotent).
50. As a developer, I want a failing hook to fail the set and trigger rollback, so that I never get a set with a half-initialised database.
51. As a repo maintainer, I want init-script volumes never to count as a dedup conflict, since repos don't declare them.

### Addressing and networking

52. As a developer, I want each hostname in a set to get its own loopback IP from the set's /24 lease (.1–.254), so that every service keeps its native port.
53. As a developer, I want each set to lease one /24 (e.g. `jira-123` → 127.42.3.0/24), so that up to 255 sets with 254 hostnames each fit in the default pool.
54. As a developer, I want every service reachable by its short alias (`mysql`) on the set's Docker network, so that committed app config works unchanged.
55. As a developer, I want every service reachable at `<service>.<set>.test` from both the host and inside the set, at the same IP and port, so that tools and humans use one address everywhere.
56. As a developer, I want ports published only on the hostname's IP (e.g. `127.42.3.5:3306:3306`), so that sets never collide.
57. As a developer, I want a set's IP leases to be sticky across `down` and `up`, so that addresses stay stable for my tools and bookmarks.
58. As a developer, I want `/etc/hosts` managed as one `# sandbox:<set>` block per set, written after the set is up, so that my other entries are never touched.
59. As a security-conscious developer, I want `sandbox-hosts` to refuse any IP outside the set's own block and any name not ending in `.test`, so that the helper can't be abused to hijack real domains.
60. As a macOS developer, I want `up` to fail clearly when all alias blocks are leased, so that I know to rerun setup with a larger `--sets`.
61. As a developer, I want the IP lease file and `/etc/hosts` guarded by `flock`, so that concurrent `sandbox` processes never corrupt them.

### Web apps and TLS

62. As a repo maintainer, I want to mark my web service with `x-sandbox: { web: true }`, so that sandbox serves it at `https://<repo>.<set>.test`.
63. As a repo maintainer, I want the web service to share the repo's name, with at most one per repo, so that URLs are predictable.
64. As a repo maintainer, I want optional extra hosts (`hosts: [admin, portal]`) routed to the same container and IP, so that multi-host apps work.
65. As a developer, I want extra host names one label deep, so that the set's wildcard certificate covers them.
66. As a developer, I want `up` to fail if a hostname is already used in the set, so that routing is never ambiguous.
67. As a repo maintainer, I want `APP_URL` set to the primary hostname and `WORKTREE` injected, so that browser-visible URLs are correct.
68. As a developer, I want apps to listen on 443 directly as non-root (via `net.ipv4.ip_unprivileged_port_start=0` on app services), so that the port is the same on the host and inside the set.
69. As a front-end developer, I want an optional `vite: 5173` published with the HMR host set to the app's hostname, so that hot reload works through the set.
70. As a developer, I want one mkcert CA per machine and a per-set `*.<set>.test` leaf certificate stored in the set directory, so that each set has valid HTTPS.
71. As a developer, I want `up` to reuse the set's certificate if it's still valid, so that resumes are fast.
72. As a security-conscious developer, I want only `rootCA.pem` mounted into containers, never `rootCA-key.pem`, so that an agent can't mint certificates my browser trusts.

### Agent container

73. As a developer, I want `sandbox SET claude` to launch Claude Code inside the set's agent container, so that the agent works in the set.
74. As a developer, I want `sandbox SET sh` to open a shell in the agent container, starting in the set's workspace.
75. As a developer, I want `sandbox SET sh --repo api` to start the shell in that repo's worktree.
76. As a developer, I want `sandbox SET sh SERVICE` to open a shell in that service's container, and an unknown name to fail and suggest `--repo`, so that I don't confuse services and repos.
77. As a developer, I want the set directory mounted at `/workspace/<set>/` (e.g. `/workspace/jira-123/api`), so that Claude session histories don't merge across sets.
78. As a developer, I want each main repo's `.git` mounted read-only at its exact host path (the clones borrow its objects there), and the main working tree never mounted, so that the agent can't write anything host git later reads or writes (accepting that other branches are visible).
79. As a developer, I want the host's `~/.claude/` bind-mounted read-write into every agent container with `CLAUDE_CONFIG_DIR=/home/agent/.claude`, so that all sets share one login and config without touching my host `~/.claude.json`.
80. As a macOS developer, I want the first `sandbox SET claude` to run `/login` inside the container, writing `.credentials.json` into the shared mount, so that every set is logged in thereafter (accepting a plaintext 0600 token and occasional re-logins).
81. As a Linux developer, I want the agent to run as my host UID:GID, with an entrypoint adding a passwd entry, so that files it writes to my worktrees and `~/.claude/` stay mine.
82. As a developer, I want the agent hardened (non-root, dropped capabilities, never the Docker socket), so that an agent can't escape to the host.
83. As a developer, I want to override the agent with a Compose fragment in config, a template or a set plan, so that I can bring my own image or an egress proxy.
84. As a developer, I want sandbox to re-apply its mounts, user, CA, `CLAUDE_CONFIG_DIR`, capability drops and no-socket rule over my override, so that customisation can't remove the safety guarantees.
85. As a developer, I want `up` to check that a custom agent image has `sh`, `git` and `claude` on PATH and fail early if not.
86. As a developer, I want other services in my agent override (e.g. a proxy) to join the set.
87. As a developer, I want egress documented as unrestricted in the MVP, so that I know to supply my own proxy if I need one.

### Inspecting sets

88. As a developer, I want `sandbox status` to list all sets with their state (running, stopped, partial, orphaned), so that I see everything at a glance.
89. As a developer, I want `sandbox SET status` to show each service's health, hostnames and IPs, and each repo's branch with a marker for uncommitted work.
90. As a developer, I want `sandbox SET urls` to list web apps as `https://` URLs and backing services as `host:port`, each with its IP.
91. As a script author, I want `--json` on `status` and `urls` to print the same models as the tables, so that I can build tooling on top.

### Resuming and idempotency

92. As a developer, I want `up` to do a full provision when no set directory exists.
93. As a developer, I want `up` to resume when the directory and plan exist but nothing is running: skip worktree creation, reuse sticky IPs, reuse a valid cert, run Compose, run hooks and write hosts entries.
94. As a developer, I want `up` on a running set to be a no-op that exits 0 and prints the bindings, so that retries are safe.
95. As a developer, I want `--recreate` to force a restart of a running set.
96. As a developer, I want creating from a template, resuming, `down` and `prune` to use the absolute paths pinned in the plan, so that the current directory only matters for `--repos`.

### Teardown and cleanup

97. As a developer, I want `sandbox SET down` to stop containers, remove the network and volumes, and remove hosts entries, while keeping worktrees and IP leases, so that I can pause a set cheaply.
98. As a developer, I want `sandbox SET prune` to clear a set's container infrastructure and release its IP leases, so that addresses return to the pool.
99. As a developer, I want a bare `sandbox prune` to clear every orphaned environment by reconciling state against Docker labels, so that crash leftovers are easy to clean.
100. As a developer, I want `down` and `prune` never to touch worktrees, so that the agent's work is safe.
101. As a developer, I want `sandbox SET destroy` to sync the agent's branches one last time (refusing, unless `--force`, agent commits it can't sync), prune, then remove the clones and the set directory including its plan and certificate.
102. As a developer, I want `destroy` to refuse if any repo has uncommitted or untracked changes unless `--force` is given.
103. As a developer, I want `prune` and `destroy` to refuse on a running set.
104. As a developer, I want `agent/<set>` branches kept by `destroy`, and deleted only with `--branches`, which refuses on unpushed commits unless `--force` is also given.
105. As a Linux developer, I want worktree removal that hits root-owned files to fall back to deleting them from a throwaway container, so that I never need sudo.

### Failure handling and concurrency

106. As a developer, I want a failed set to roll back its own infrastructure and leave existing worktrees in place, so that I can fix the cause and rerun `up`.
107. As a developer, I want rollback to run shielded from cancellation on Ctrl-C, so that interrupting never leaves orphans.
108. As a developer, I want sets independent, so that one set's failure never cancels another.
109. As a developer, I want the steps inside a set to fail fast, so that a broken set doesn't waste time.

### Engineering quality (portfolio)

110. As a hiring manager reading the repo, I want Python 3.12+, uv, ruff, strict pyright in CI, Pydantic v2, Typer and structured asyncio, so that the code reads as senior-level.
111. As a contributor, I want unit tests on Linux and macOS, integration tests against real git and Docker on Linux, and an end-to-end suite on a sudo-capable Linux runner, so that every layer is covered.

## Implementation Decisions

### CLI surface

Commands take the shape `sandbox [SET] <verb>`. Verb names are reserved and cannot be set names.

```
sandbox SET up [--from TEMPLATE | --repos NAME...] [--recreate]
sandbox SET down                               # container infra; syncs agent/<set>; clones stay
sandbox SET destroy [--branches] [--force]     # sync + prune + remove clones and the set directory
sandbox [SET] prune                            # no SET: every orphaned environment
sandbox [SET] status [--json]                  # state, hostnames and IPs
sandbox SET claude                             # launch Claude in the agent container
sandbox SET sh [--repo NAME | SERVICE]         # agent shell, repo worktree, or a service container
sandbox SET urls [--json]                      # host-resolvable URLs for the set
sandbox [SET] save --to TEMPLATE [--repos NAME...]
sandbox templates
sudo sandbox setup [--uninstall] [--sets N]    # one-time: hosts helper, macOS loopback pool, mkcert CA
```

### Plan schema (templates and set plans share it)

From the concept doc:

```yaml
# ~/sandbox/sets/jira-123/sandbox.yaml
repos:
  api: { source: /home/ollie/code/api, base: main }
  web: { source: /home/ollie/code/web, base: release/2.4 }
# agent: <optional Compose fragment, pinned like source>
```

`source` is absolute. `base` is required in a set plan and optional in a template. There is no template inheritance and no repo registry.

### Repo opt-in contract

A repo takes part if its root has `.sandbox/`. Inside it, `compose.yaml` and `init/<service>.sh` are optional. Web apps are declared with the `x-sandbox` extension (from the concept doc):

```yaml
services:
  api:
    build: ..
    x-sandbox:
      web: true              # https://api.<set>.test on 443; WORKTREE + APP_URL injected
      hosts: [admin, portal] # optional extra one-label hosts, same container and IP
      vite: 5173             # optional Vite dev server, HMR host set
```

### Modules

Provisioning side:

- **Provisioner**: orchestrates `up`, `down`, `prune`, `destroy` across sets. Decides the action from the set's on-disk state using the state table below. Owns rollback.
- **DependencyResolver**: normalises each repo's compose file on its own with `docker compose config`, merges same-named services under the dedup rule, and fails with a diff naming both repos. Produces a `ServiceGraph`.
- **OverrideRenderer**: turns a `ServiceGraph`, `HostBinding`s and the agent definition into the per-set `override.yaml`: both aliases per service, IP-bound ports, the agent service (default merged with any override, then sandbox's guarantees re-applied), worktree/`.git`/CA/`~/.claude/` mounts, init-hook mounts, `WORKTREE` and `APP_URL`, the unprivileged-port sysctl on app services, and the host UID:GID on the agent.
- **ComposeProject**: wraps the Compose CLI via subprocess for one project `sandbox-<set>` (`config`, `up -d --wait`, `exec`, `down -v`, `ps`). No Docker SDK.

Host-state side (Compose can't manage these; each takes its file paths through its constructor):

- **CloneManager** (was WorktreeManager): creates the agent's clone of each repo on `agent/<set>` and syncs that branch into the source repo, serialised per repo; dirty/untracked, unsynced and unpushed checks (in destroy); container-based fallback removal.
- **IpPool**: leases one /24 per set and one IP per hostname within it, sticky until `prune`; `flock`-guarded lease file.
- **CertAuthority**: wraps mkcert to issue and validate per-set `*.<set>.test` certificates; checks `rootCA.pem` exists.
- **HostsFile**: writes and removes the `# sandbox:<set>` block, via `sudo -n sandbox-hosts` in production.

Value objects: `ServiceGraph` (merged service specs by name), `HostBinding` (hostname and loopback IP), `SetResult` (bindings or error). `status`/`urls` output models are Pydantic models shared by the table and `--json` renderers.

### Deduplication rule

Services with the same name are the same service; no matching by image. After per-repo normalisation the definitions must be identical, except:

- init-script volumes never conflict (sandbox adds them);
- `healthcheck` kept if only one repo sets it, conflict if two differ;
- `depends_on`, `labels`, `ports` and `x-sandbox` are ignored.

Anything else fails with a diff naming both repos. Multi-version services are out of scope.

### Lifecycle

`up` per set: create worktrees → resolve and validate deps → lease IPs → issue cert → render override → `compose up -d --wait` → run init hooks → write hosts block. Validation (plan, `.sandbox/`, dedup, hostname clashes, agent image commands, setup prerequisites) completes before any container starts.

State table (the set directory is the source of truth):

| State found | `up` does |
|---|---|
| No directory | Full provision |
| Directory and plan, nothing running | Resume: skip worktrees, reuse sticky IPs, reuse valid cert, Compose, hooks, hosts |
| Already running | No-op, exit 0, print bindings; `--recreate` restarts |
| Different plan passed than on disk | Refuse; disk wins |

Set states reported by `status`: running, stopped, partial, orphaned. Orphans are found by reconciling set directories and lease file against Docker labels.

### Addressing

- Pool 127.42.0.0/16 by default (`ip_pool` in config); one /24 per set; hostnames take .1–.254.
- Two names per service: short alias on the set network, and `<name>.<set>.test` as network alias plus `/etc/hosts` entry.
- Native ports published on the hostname's IP. Apps on 443, non-root.
- `sandbox-hosts` rewrites only its set's block and refuses IPs outside the set's /24 and names not ending in `.test`.

### Init hooks

`<repo>/.sandbox/init/<service>.sh` mounted read-only at `/sandbox/init/<repo>/` in that service, executed with `docker compose exec` once healthy, in repo-name order, on every `up`. Failure fails the set and rolls back.

### Agent container

Hardened: non-root, capabilities dropped, no Docker socket. Mounts: set directory at `/workspace/<set>/` (shell starts there), each main repo's `.git` read-write at its exact host path, host `~/.claude/` read-write, `rootCA.pem` only. `CLAUDE_CONFIG_DIR=/home/agent/.claude`. User set to host UID:GID; entrypoint adds a passwd entry. Custom images must provide `sh`, `git`, `claude` on PATH and work under any UID with writable HOME. The `.git` exposure (all branches visible to the agent) is accepted. Egress is unrestricted in the MVP.

### Concurrency

Each process provisions one set. Within a set, steps run in a `TaskGroup` and fail fast. Cross-process shared files (lease file, `/etc/hosts`) are `flock`-guarded; creating a clone and syncing `agent/<set>` are serialised per repo. Rollback is shielded from cancellation. `fcntl` locking means Linux and macOS only. (The concept's "sets in parallel under a semaphore with gather" applies to any internal multi-set operation such as bare `prune`; multi-set `up` is out of scope.)

### Filesystem layout

- Config: `~/.config/sandbox/config.yaml` (`ip_pool`, templates root, sets root, default `agent`).
- Templates: `~/.config/sandbox/plans/`.
- Sets: `~/sandbox/sets/<set>/` holding `sandbox.yaml`, the rendered override, the leaf certificate, and the agent's clone of each repo.

### Toolchain

Python 3.12+, uv with `pyproject.toml`, ruff, pyright strict enforced in CI, Pydantic v2, Typer, asyncio with structured concurrency, pytest.

## Testing Decisions

### What makes a good test

Tests exercise external behaviour through public seams: CLI exit codes and output, the files sandbox writes (plan, override, lease file, hosts file), and the observable state of git and Docker. They don't assert on private methods, call order or internal data structures, so the internals can be refactored freely.

### Proposed seams (please confirm)

The repo is empty, so there is no prior art and no existing seams. Proposed, as high as possible and as few as possible:

1. **Primary seam: the `sandbox` CLI app**, invoked in-process (Typer's test runner) with a config that points every host-state location at a temp directory: sets root, templates root, lease file, hosts file (written directly instead of via `sandbox-hosts`), and CAROOT. Nearly all behaviour is asserted here: state-table decisions, validation errors, rollback, destroy safety checks, status/urls output, lease stickiness. Unit-speed runs swap Git/Compose/mkcert for fakes behind this seam; integration runs use real git and Docker through the same seam.
2. **Secondary seam: `DependencyResolver`** (repo compose files in → `ServiceGraph` or conflict diff out). Justified only because the dedup rule has many combinations that would be slow and noisy to drive through the CLI with Docker.

`OverrideRenderer` output is checked through the CLI seam by inspecting the rendered override file (and `docker compose config` on it in integration), rather than as a third seam.

### Layers

| Layer | Covers | Runs on |
|---|---|---|
| Unit | plan parsing, dedup and diff, override rendering, lease allocation, state-table decisions — against fakes | every push, Linux and macOS |
| Integration | real worktrees, `compose up --wait`, init hooks, destroy safety checks; hosts writes to a temp file | every push, Linux |
| End to end | `sudo sandbox setup`, `up`, HTTPS from host and agent, `down`, `destroy` | Linux runner with passwordless sudo; macOS by hand before each release |

### Fixtures

A pair of fixture repos that share `mysql`, one of which declares a web app with an extra host and an init hook, plus a conflicting variant for the dedup tests.

## Out of Scope

- Multi-version services (e.g. `mysql84` beside `mysql`); conflicting duplicates fail.
- Multi-set `up` in a single command.
- Template inheritance.
- A repo registry; repos come from the current directory or pinned absolute paths.
- Network egress restriction for the agent (documented gap; bring your own proxy via agent override).
- Hiding other branches of a repo from the agent (read-write `.git` exposure is accepted).
- Windows support.
- Carrying host MCP servers or the host `~/.claude.json` into the agent.
- Automatic `mkcert -uninstall`.

## Further Notes

- Known accepted trade-offs: plaintext Claude token on macOS; concurrent token refreshes may occasionally force a re-login; the agent can see every branch of each mounted repo.
- The `.test` TLD is reserved for this kind of use and permits wildcard certificates.
- The project doubles as a portfolio piece for AI platform and agentic infrastructure roles, so code quality, typing and test layering are part of the acceptance bar, not extras.
- Before implementation, confirm the two proposed test seams above.
