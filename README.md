# agentic-environment-provisioning

`sandbox` provisions isolated **sets** for coding agents: a git worktree per repo on
branch `agent/<set>`, deduplicated backing services, and a hardened agent container.
See [`.scratch/sandbox-mvp/spec.md`](.scratch/sandbox-mvp/spec.md) for the full MVP spec.

Status: early MVP. `sandbox SET up --repos NAME...` creates the set directory, writes its
plan, cuts the worktrees, then starts the services a repo declares in
`.sandbox/compose.yaml` as Compose project `sandbox-<set>` and waits until they are healthy
(a repo without `compose.yaml` takes part with its worktree only). Inside the set, services
reach each other by their short names (`mysql`). `sandbox SET down` stops the containers and
removes the set's network and volumes; worktrees and the plan are kept. Repos that declare
the same service share one instance of it (see [Shared services](#shared-services)). Once
the services are up, `up` writes the set's hostnames into the hosts file (see
[Hosts file](#hosts-file)); `down` removes them.

```console
$ cd ~/code            # holds ./api and ./web, each with a .sandbox/ directory
$ sandbox jira-123 up --repos api web
set jira-123: /home/me/sandbox/sets/jira-123
  plan: /home/me/sandbox/sets/jira-123/sandbox.yaml (created)
  api: /home/me/sandbox/sets/jira-123/api [agent/jira-123, created from main]
  web: /home/me/sandbox/sets/jira-123/web [agent/jira-123, created from release/2.4]
  services: mysql healthy [sandbox-jira-123]
  agent: running [sandbox-jira-123]; open a shell: sandbox jira-123 sh
$ sandbox jira-123 sh --repo api   # a shell in the agent, in /workspace/jira-123/api
$ sandbox jira-123 sh mysql        # a shell in the mysql service container
$ sandbox jira-123 claude          # Claude Code in the agent, in /workspace/jira-123
$ sandbox jira-123 down
set jira-123: sandbox-jira-123 stopped; worktrees kept in /home/me/sandbox/sets/jira-123
```

`up` renders a per-set `override.yaml` into the set directory and runs
`docker compose -p sandbox-<set> -f <api>/.sandbox/compose.yaml -f <web>/... -f override.yaml up -d --wait`,
with every repo's original file (in repo-name order) before the override. The override
labels every service, network and declared volume with `sandbox.set=<set>`.

### Shared services

Services are matched **by name only** (never by image): if `api` and `web` both declare
`mysql`, the set runs one `mysql`. Before anything is created, each repo's compose file is
normalised on its own with `docker compose config` (so `extends`, short/long syntax and
list/map `environment` don't cause false conflicts) and same-named services are compared:

- `depends_on`, `labels`, `ports` and `x-sandbox` are ignored (sandbox rewrites ports), as are
  init-hook mounts under `/sandbox/init/` (see below);
- a `healthcheck` set by only one repo is kept; two different healthchecks conflict;
- any other difference fails `up`, before any container starts, with a diff naming both repos:

```console
$ sandbox jira-123 up --repos api legacy
error: service 'mysql' is defined differently by api and legacy:
  --- api: mysql
  +++ legacy: mysql
  @@ -6,7 +6,7 @@
  ...
  -image: mysql:8.4
  +image: mysql:8.0
```

**Multi-version services are not supported in the MVP.** Two repos that need different
versions of a service (e.g. `mysql84` beside `mysql`, or `mysql:8.0` vs `mysql:8.4` under
one name) cannot share a set; run them in separate sets.

Compose resolves the relative paths of *every* `-f` file from the first file's directory,
so for a service that only a later repo declares, the override restates that repo's
normalised definition (absolute paths, `env_file` inlined into `environment`) with
Compose's `!override`/`!reset` merge tags (Docker Compose 2.24 or newer). One limitation
remains: `extends: {file: ...}` is resolved while Compose loads the files, so only the
first repo (by name) of a multi-repo set may use it; later repos should extend services
defined in the same file.

### Init hooks

A repo can ship `.sandbox/init/<service>.sh` to create its own database, users or seed data
in a (possibly shared) service. Sandbox mounts the repo's `.sandbox/init/` read-only at
`/sandbox/init/<repo>/` in that service and, once `compose up --wait` reports the set
healthy, runs `sh /sandbox/init/<repo>/<service>.sh` with `docker compose exec`, in
repo-name order. Hooks run on every `up` that starts the set (first `up`, resume,
`--recreate`), so write them to be idempotent:

```sh
# api/.sandbox/init/mysql.sh
mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS api"
```

A hook for a service the set does not run fails `up` before any container starts. A hook
that exits non-zero fails the set and rolls it back (plan and worktrees are kept); the
error shows the hook's output. Hooks are read from the repo's source checkout, like its
compose file.

### HTTPS web apps

A repo marks its web service with the `x-sandbox` extension. The service must be named after
the repo, and a repo has at most one:

```yaml
# web/.sandbox/compose.yaml
services:
  web:
    build: ..
    x-sandbox:
      web: true # served at https://web.<set>.test
      hosts: [admin, portal] # optional: https://admin.<set>.test, https://portal.<set>.test
      vite: 5173 # optional: Vite dev server port
```

Sandbox then:

- serves it at `https://<repo>.<set>.test`: the service's own leased IP, with port 443
  published on that IP. The app listens on 443 itself, as non-root, because the service
  gets `net.ipv4.ip_unprivileged_port_start=0`;
- issues a per-set `*.<set>.test` certificate with mkcert into `<set dir>/tls/` and mounts
  it read-only at `/sandbox/tls/cert.pem` and `/sandbox/tls/key.pem` (the key is readable
  by any user, so the app can run as whatever user its image uses). Later `up`s reuse it
  while it verifies against the CA and has more than 7 days left;
- injects `WORKTREE` (the repo's worktree, `<set dir>/<repo>`) and `APP_URL`
  (`https://<repo>.<set>.test`) into the service's environment, and nothing else (plus
  `VITE_HMR_HOST` with `vite`, below);
- routes each extra host in `hosts` (`admin` becomes `admin.<set>.test`) to the same
  container and IP: it is a network alias of the service, gets a line in the set's hosts
  block, and is covered by the set's wildcard certificate. That is why an extra host must
  be a single DNS label (`admin`, not `eu.admin`);
- with `vite: <port>`, publishes that port on the app's IP as well and sets `VITE_HMR_HOST`
  to the app's hostname. Point Vite's HMR at it, e.g. in `vite.config.js`:
  `server: { host: true, port: 5173, hmr: { host: process.env.VITE_HMR_HOST } }`;
- mounts the mkcert CA's `rootCA.pem`, never `rootCA-key.pem`, read-only at
  `/sandbox/ca/rootCA.pem` in every container of the set. The bundled agent image trusts it
  system-wide (curl, git) and through `NODE_EXTRA_CA_CERTS`; a service image decides itself
  whether to trust it.

The host trusts the certificate once `sudo sandbox setup` has installed the mkcert CA
(`mkcert -install`). `up` fails before any container starts if a set with a web app finds
no `rootCA.pem` in mkcert's `CAROOT`, if a repo marks more than one service or a service not
named after the repo, if an extra host is more than one label deep, or if a hostname is used
twice in the set (e.g. another repo runs a service with the web app's name, or an extra
host matches a service or another web app). Sets without a web app need neither mkcert nor the CA.

### The agent container

Every set also runs a hardened `agent` service (a repo may not declare a service of that
name). `sandbox SET sh` opens a shell in it at `/workspace/<set>/`; `--repo NAME` starts in
that repo's worktree. `sandbox SET sh SERVICE` opens a shell in one of the set's services
instead; an unknown name fails and points at `--repo`.

- **Image**: the bundled default (`src/sandbox/images/agent/`: Ubuntu 24.04 with `sh`,
  `git` and `claude` on PATH). Compose builds it on the first `up` as
  `sandbox-agent:<content hash>` and never pulls it from a registry. It works under any
  UID with a writable `HOME` (`/home/agent`): its entrypoint adds a passwd/group entry for
  the UID:GID it runs as.
- **User**: your host UID:GID, so files the agent writes in the worktrees and in
  `~/.claude/` stay yours. If sandbox itself runs as root, the agent runs as `1000:1000`;
  it never runs as root.
- **Hardening**: `cap_drop: [ALL]`, `no-new-privileges`, and the Docker socket is never
  mounted.
- **Mounts** (read-write binds unless noted): the set directory at `/workspace/<set>/`
  (its `sandbox.yaml` read-only); each source repo's `.git` at its exact host path,
  because a worktree's `.git` file points there (so the agent can see every branch of the
  repo), **read-only**, with read-write binds over only what committing, branching and
  fetching in the set's worktree write: `objects/`, `refs/`, `logs/` (`reftable/` in
  repos that use it) and the set's own `worktrees/<name>/`. Config and hooks there are
  what git on the host reads, so the agent can't change them, even after host git
  replaces `.git/config` (it renames a new file over it). So in the agent `git config`,
  `git push -u`, deleting a packed branch and `git gc` fail; commit, branch, fetch and
  push work. Independently, every git command sandbox runs on the host uses isolated
  config (no system or global config, nothing from `GIT_*` variables, and hooks,
  fsmonitor, editors, pagers and other program-running settings pinned off), reads
  nothing from a worktree's git dir but its `HEAD` and index, and ignores any `.git` or
  `commondir` file the agent planted. (So filters such as Git LFS's, configured in your
  global config, don't run when sandbox creates a worktree.) The worktrees themselves are
  the agent's: treat running git in one on the host like running it in a repo you
  cloned from a stranger. Also: the host's `~/.claude/` at
  `/home/agent/.claude` with `CLAUDE_CONFIG_DIR=/home/agent/.claude`, so all sets share one
  login. Your main working trees and `~/.claude.json` are never mounted. In a set with a
  web app, the mkcert CA's `rootCA.pem` is also mounted read-only (see above).

### Claude Code in the agent

`sandbox SET claude` runs Claude Code interactively in the set's agent, starting in
`/workspace/<set>/`. It fails with a clear message (and the `up` command to run) if the set
isn't running. Every set shares one Claude login through the `~/.claude/` mount: the first
time no `~/.claude/.credentials.json` exists, sandbox says so and Claude Code leads you
through `/login`, which writes `.credentials.json` into that directory; every other set
then starts already logged in. sandbox keeps that file at mode `0600`.

```console
$ sandbox jira-123 claude    # first time: log in with /login
$ sandbox jira-456 claude    # already logged in
```

Accepted trade-offs:

- **Plaintext token on macOS.** Claude Code on a Mac host keeps its login in the Keychain,
  which a Linux container cannot reach, so the agents log in separately and store their
  token in plaintext in `~/.claude/.credentials.json` (owner-only, `0600`), where any process
  running as you can read it. (On Linux hosts Claude Code already uses
  this file.)
- **Occasional forced re-login.** Concurrent sets share one token file. When two agents
  refresh the token at the same moment, one refresh can invalidate the other's token, and
  that session has to `/login` again. Logging in again from any set fixes it for all.

Linux and macOS only.

### Agent overrides

Bring your own agent image, or add sidecars such as an egress proxy, with an `agent`
Compose fragment (keys `services`, `volumes`, `networks`) in the global config, a template
or a set plan. The set plan wins, then the template, then the config: whichever applies
when the set is created is **pinned into its plan**, like `source`, so later config edits
don't change an existing set (edit its `sandbox.yaml` instead).

`services.agent` is merged over the default agent, then sandbox **re-applies its
guarantees** on top: the mounts above (plus the CA certificate, once sets serve HTTPS),
the host UID:GID as `user`, `CLAUDE_CONFIG_DIR` and `HOME`, `cap_drop: [ALL]` and
`no-new-privileges`. An override can't run the agent as root, add capabilities
(`privileged`, `cap_add`, `devices`, `pid`/`ipc`/`userns_mode`, `network_mode`,
`volumes_from` are removed), or mount the Docker socket (a `docker.sock` or a directory
holding one, such as `/var/run`). `up` warns about each setting it overrides. Extra
volumes and environment variables are kept.

A custom `image` replaces the bundled one and is pulled like any other image (`build` is
not supported: build it yourself and set `image`). It must work under any UID with a
writable `HOME` and have `sh`, `git` and `claude` on its PATH: `up` runs it once with
`docker run` to check, and fails before any container starts if one is missing.

Every other service in the fragment runs with the set, labelled with it, and joins the
set network (its `network_mode` is dropped; `default` is added to its `networks`), so the
agent and the repos' services reach it by name. A fragment service may not reuse the name
of a repo's service.

**Egress is unrestricted in the MVP**: the agent can reach any host the Docker host can.
To restrict it, bring your own proxy, for example:

```yaml
# ~/.config/sandbox/config.yaml (or under `agent:` in a template or set plan)
agent:
  services:
    agent:
      environment:
        HTTPS_PROXY: http://proxy:3128
        HTTP_PROXY: http://proxy:3128
        NO_PROXY: localhost,127.0.0.1,mysql
    proxy:
      image: ubuntu/squid:6.6-24.04_edge
      volumes:
        - /home/me/sandbox/squid.conf:/etc/squid/squid.conf:ro   # your allowlist
```

Proxy variables are honoured by `git`, `claude` and most tools, but nothing stops a
process from ignoring them: the agent stays on the set network, which has a route out.
Enforced egress restriction is out of scope for the MVP.

## Status

`sandbox status` lists every set with its state; `sandbox SET status` shows one set's
services (container state, health, hostname, leased IP) and repos (branch, plus `*` for
uncommitted changes and `?` for untracked files). `--json` prints the same Pydantic models
the tables are rendered from.

```console
$ sandbox status
SET       STATE     DETAIL
jira-123  running
jira-456  partial   not running: redis
spike-01  orphaned  2 containers and an IP lease with no set directory
$ sandbox jira-123 status
set jira-123: running [sandbox-jira-123]

SERVICE  STATE    HEALTH   HOSTNAME             IP
agent    running  -        -                    -
mysql    running  healthy  mysql.jira-123.test  127.42.0.1

REPO  BRANCH          CHANGES
api   agent/jira-123  * uncommitted changes
web   agent/jira-123  clean
```

## URLs

`sandbox SET urls` lists every address in a set you can reach from the host: web apps as
`https://` URLs (extra hosts and a Vite dev server included) and backing services as
`<service>.<set>.test:<port>`, each with its leased loopback IP. A stopped set lists the
addresses from its sticky lease, marked as not running. `--json` prints the same Pydantic
model the table is rendered from.

```console
$ sandbox jira-123 urls
set jira-123: running

WEB                                 IP
https://web.jira-123.test           127.42.0.3
https://admin.jira-123.test         127.42.0.3
https://web.jira-123.test:5173      127.42.0.3

SERVICE                             IP
mysql.jira-123.test:3306            127.42.0.1
redis.jira-123.test:6379            127.42.0.2
```

States are reconciled from the set directories, the lease file and the containers Docker
labels `sandbox.set=<set>` (queried with the `docker` CLI): **running** (every service and
the agent running), **stopped** (a plan, nothing running), **partial** (some running) and
**orphaned** (containers or an IP lease whose set has no plan, or a set directory without
one).

## Prune

`sandbox SET prune` removes a stopped set's containers, network, volumes and hosts block,
and releases its IP lease back to the pool; the next `up` leases it a fresh block. It
refuses while any of the set's services run (`sandbox SET down` first), or while an `up`
of the set is in progress. The set directory, plan and worktrees are never touched.

Bare `sandbox prune` does the same for every **orphaned** set `status` reports (leftovers
of crashes). A set whose `up` is still in progress is skipped: a brand-new set holds its
lease before its plan exists, and a first `up` that fails before writing the plan gives
that lease back itself. Sets are pruned concurrently (at most 4 at a time) and independently: a set
that fails is reported, keeps its lease so a rerun retries it, and the rest carry on; the
command then exits 1.

## Destroy

`sandbox SET destroy` removes a set for good: it prunes it (as above), removes each
worktree with `git worktree remove` (so the source repos keep no stale worktree metadata),
and deletes the set directory with its plan and certificate. Every check runs before
anything is removed, so a refused `destroy` changes nothing:

- a set with anything running is refused, even with `--force` (`sandbox SET down` first);
- a worktree with uncommitted changes or untracked files is refused, naming the repo and
  the paths, unless `--force` (which discards them);
- the `agent/SET` branches are kept by default. `--branches` deletes them too, but refuses
  a branch holding commits that are on no remote-tracking branch, other branch or tag
  (named in the error) unless `--force` is also given.

Containers can leave root-owned files in a worktree (a service writing to a bind mount as
root). On Linux, `destroy` deletes those from a throwaway root container
(`docker run --rm ubuntu:24.04 ...`, the default agent image's base), so sudo is never
needed. On macOS Docker Desktop maps container files to the developer, so this never arises.

## Setup

Everything that needs root is installed once, after which `sandbox` runs unprivileged:

```console
$ sudo sandbox setup              # or: sudo "$(command -v sandbox)" setup, if sudo's PATH lacks it
sandbox setup installed:
  hosts helper: /usr/local/bin/sandbox-hosts (root-owned), runnable by me without a password (sudoers rule /etc/sudoers.d/sandbox-hosts, this helper only)
  loopback: no loopback setup needed on Linux (127.0.0.0/8 is local)
  mkcert: CA installed for me (`mkcert -install` as me)
$ sudo sandbox setup --uninstall
```

- **Hosts helper.** `sandbox-hosts` (see [Hosts file](#hosts-file)) is installed root-owned
  at `/usr/local/bin/sandbox-hosts`. It runs under the system Python (`/usr/bin/python3`,
  3.9 or newer) with `-I -S`, from a root-owned copy of the few standard-library-only modules
  it needs in `/usr/local/lib/sandbox-hosts/`, so nothing you can write (a virtualenv,
  site-packages, `PYTHONPATH`) ever runs as root. A NOPASSWD sudoers rule
  (`/etc/sudoers.d/sandbox-hosts`, checked with `visudo -c` first) lets you, and only you,
  run exactly that file.
- **macOS loopback pool.** On macOS only 127.0.0.1 itself answers on `lo0`, so setup
  installs a launchd job (`/Library/LaunchDaemons/sandbox.loopback-aliases.plist`) that
  adds an `lo0` alias for every address of the first `--sets N` /24 blocks of `ip_pool` at
  boot (default 16, so 16 sets can run at once), and adds them right away too. Rerunning setup resizes the pool. On
  Linux no loopback setup is performed: the whole of 127.0.0.0/8 already works.
- **mkcert CA.** `mkcert -install` runs as you (`SUDO_USER`), never as root, so the CA lands
  in your own CAROOT and your browser trusts it. mkcert must be on sudo's `PATH`.
- Setup reads **your** config (`~/.config/sandbox/config.yaml`) and refuses, before
  installing anything, if any address in `ip_pool` is already in use: assigned to a network
  interface, or mapped by a hosts-file line outside sandbox's own blocks.
- `--uninstall` removes the helper, its sudoers rule and the launchd job (and the aliases),
  but leaves the mkcert CA, which other tools may use; it prints `mkcert -uninstall` for you
  to run yourself if you want it gone.

`up` checks all of this before it creates anything (even before leasing IPs), and names
what is missing: the hosts helper or its sudoers rule (when `hosts_file` is unset), on macOS
the alias pool, and for a set with a web app the mkcert `rootCA.pem`. Sets without services
need none of it. On macOS a set only leases blocks that are aliased; when every one is
taken, `up` fails and suggests rerunning setup with a larger `--sets`.

The macOS launchd/`lo0` path is unit-tested against a fake host (the generated plist and
its alias script are run with a stand-in `ifconfig`), but only a person on a Mac with sudo
can verify it for real: run `sudo sandbox setup --sets 2`, check `ifconfig lo0` lists
127.42.0.1-127.42.1.254, reboot and check again, run `up` for a set with services, then
`sudo sandbox setup --uninstall` and check the aliases and the plist are gone. The full list is
the [macOS release checklist](docs/release-checklist-macos.md).

## Configuration

`~/.config/sandbox/config.yaml` (override the path with `SANDBOX_CONFIG`). Every key is
optional; unknown keys and bad values fail with field-level errors.

```yaml
ip_pool: 127.42.0.0/16            # loopback range; each set leases one /24
templates_root: ~/.config/sandbox/plans
sets_root: ~/sandbox/sets
state_dir: ~/.local/state/sandbox # lock files and other cross-process host state
hosts_file: null                  # unset: /etc/hosts via `sudo -n sandbox-hosts` (see Hosts file)
agent: null                       # default agent override (see Agent overrides)
```

## Hosts file

After a set's services are up, `up` writes one block per set to `/etc/hosts`, holding
exactly that set's hostnames and their leased IPs. Nothing outside a set's own block is
touched; `down` (and a rolled-back `up`) removes the block.

```text
# sandbox:jira-123
127.42.0.1	mysql.jira-123.test
127.42.0.2	redis.jira-123.test
# end sandbox:jira-123
```

`sandbox` writes it non-interactively through `sudo -n /usr/local/bin/sandbox-hosts`, a small
helper that `sudo sandbox setup` installs root-owned (see [Setup](#setup)), with a NOPASSWD sudoers rule scoped to it. The
helper only ever edits `/etc/hosts`, rewrites only the named set's block under an
exclusive `flock` (so concurrent sets never corrupt the file), and refuses any IP outside
the set's own /24 and any name that is not a `<label>.<set>.test` hostname, so it cannot be
used to redirect a real domain. Setting `hosts_file` to a path makes `sandbox` write that
file directly instead, without sudo; the tests use this with a temp file.

A set's plan lives at `<sets_root>/<set>/sandbox.yaml`. The set's name is its directory
name and never appears in the plan. `up` never overwrites a plan and refuses a `--repos`
list that differs from the one on disk; edit the plan to change the set.

```yaml
repos:
  api: { source: /home/me/code/api, base: main }
  web: { source: /home/me/code/web, base: release/2.4 }
```

## Templates

A template is a plan stored as `<templates_root>/<NAME>.yaml`. It shares the set plan's
schema, except that `base` is optional. There is no template inheritance: each template is
a complete plan.

```console
$ sandbox save --to stack --repos api web   # from ./api and ./web; absolute sources, no base
$ sandbox jira-123 save --to stack-pinned   # an existing set's plan, bases included
$ sandbox templates                          # list them
stack
stack-pinned
$ sandbox jira-456 up --from stack          # works from any directory
```

`up --from TEMPLATE` copies the template into the new set's plan. A repo without `base`
gets the branch checked out in its source at that moment, pinned into the set plan so the
set stays reproducible. Sources are the template's absolute paths, so the current
directory does not matter. Every repo is checked (`.sandbox/` present, git repo root)
before anything is created. `--from` and `--repos` are mutually exclusive, and `save`
never overwrites an existing template.

## Development

```console
$ uv sync                          # Python 3.12+, managed by uv
$ uv run ruff format --check . && uv run ruff check .
$ uv run pyright                   # strict mode
$ uv run pytest -m "not integration"   # unit: fakes, runs on Linux and macOS
$ uv run pytest -m integration         # integration: real git + Docker Compose (Linux in CI)
$ SANDBOX_E2E=1 uv run pytest -m e2e   # end to end: sudo setup, real /etc/hosts (throwaway runner only)
```

CI (`.github/workflows/ci.yml`) enforces all of the above except the end-to-end suite.

The **end-to-end suite** (`tests/e2e/`) drives the installed `sandbox` CLI as a user would,
the privileged parts included: `sudo sandbox setup`, then two sets brought `up` in parallel
(each with two repos sharing `mysql`, one with an init hook, and a web app with an extra
host), HTTPS by hostname through the real `/etc/hosts` and the installed mkcert CA from the
host and from the agent, `down`, `destroy` and `sudo sandbox setup --uninstall`. It changes
the machine it runs on (the helper, a sudoers rule, the mkcert CA, `/etc/hosts`), so it runs
only with `SANDBOX_E2E=1`, as a non-root user with passwordless sudo. Its workflow
(`.github/workflows/e2e.yml`) runs it on a GitHub-hosted Ubuntu runner nightly, on demand
and for `v*` tags, not on every push. Before each release a person also works through the
[macOS release checklist](docs/release-checklist-macos.md).

### Conventions

- **Docker is driven through its CLIs only.** All Docker interaction goes through the
  `docker` and `docker compose` command-line tools via `subprocess` (asyncio). The Python
  Docker SDK (`docker` on PyPI) is not, and must not become, a dependency.
- **Git is driven through the `git` CLI** behind the `sandbox.git.Git` protocol, so unit
  tests can substitute `tests.fakes.FakeGit`.
- **Compose is driven through `docker compose`** behind the `sandbox.compose.cli.Compose`
  protocol; unit tests substitute `tests.fakes_compose.FakeCompose`. Compose code lives in
  the `sandbox.compose` package, one module per concern (`cli`, `services`, `override`,
  ...); a new concern adds a module and registers its fragment in `override.FRAGMENTS`.
- **One module per verb**: verbs other than `up` live in `sandbox/commands/<verb>.py` and
  are registered in `sandbox/cli.py` with one `app.command` line; shared helpers
  (`Deps`, `current_set`, error exits) are in `sandbox/context.py`.
- **Primary test seam: the CLI, in-process.** Tests call
  `CliRunner().invoke(app, args, obj=Deps(git=..., compose=...))` (see `tests/conftest.py`) with
  `SANDBOX_CONFIG` pointing at a config that puts every host-state location (sets root,
  templates root, state dir) in a temp directory. Unit runs pass fakes in `Deps`;
  integration runs (`@pytest.mark.integration`) pass the real implementations through the
  same seam. Assert on exit codes, output and the files sandbox writes, not on internals.
- **`up` is an ordered step pipeline** (`sandbox/pipeline.py`). A new check or action is a
  `Step(name, run, rollback=...)` in its own module plus one line in
  `provisioner.VALIDATION_STEPS` (checks that create nothing; all finish before any container
  starts) or `provisioner.PROVISION_STEPS` (actions; on failure or Ctrl-C the started ones roll
  back in reverse order, shielded from cancellation, keeping plan and worktrees). Per-repo
  fan-out uses `pipeline.fail_fast` (a `TaskGroup`). The state table is in `sandbox/state.py`.
- **Host state is shared across processes**, so writes to it are guarded by `flock`
  (`sandbox.locking.file_lock`); `git worktree add` is serialised per source repo.
- **User-facing failures raise `SandboxError`**; the CLI prints `error: <message>` and
  exits 1 (usage errors exit 2). No tracebacks for expected failures.
