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
the same service share one instance of it (see [Shared services](#shared-services)). No
loopback IPs or hosts entries yet.

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

- `depends_on`, `labels`, `ports` and `x-sandbox` are ignored (sandbox rewrites ports);
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
- **Mounts** (all read-write binds): the set directory at `/workspace/<set>/`; each source
  repo's `.git` at its exact host path, because a worktree's `.git` file points there (so
  the agent can see every branch of the repo); the host's `~/.claude/` at
  `/home/agent/.claude` with `CLAUDE_CONFIG_DIR=/home/agent/.claude`, so all sets share one
  login. Your main working trees and `~/.claude.json` are never mounted.

Linux and macOS only.

## Configuration

`~/.config/sandbox/config.yaml` (override the path with `SANDBOX_CONFIG`). Every key is
optional; unknown keys and bad values fail with field-level errors.

```yaml
ip_pool: 127.42.0.0/16            # loopback range; each set leases one /24
templates_root: ~/.config/sandbox/plans
sets_root: ~/sandbox/sets
state_dir: ~/.local/state/sandbox # lock files and other cross-process host state
agent: null                       # default agent override (a Compose service fragment)
```

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
```

CI (`.github/workflows/ci.yml`) enforces all of the above.

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
