# agentic-environment-provisioning

`sandbox` provisions isolated **sets** for coding agents: a git worktree per repo on
branch `agent/<set>`, deduplicated backing services, and a hardened agent container.
See [`.scratch/sandbox-mvp/spec.md`](.scratch/sandbox-mvp/spec.md) for the full MVP spec.

Status: early MVP. `sandbox SET up --repos NAME...` creates the set directory, writes its
plan, cuts the worktrees, then starts the services a repo declares in
`.sandbox/compose.yaml` as Compose project `sandbox-<set>` and waits until they are healthy
(a repo without `compose.yaml` takes part with its worktree only). Inside the set, services
reach each other by their short names (`mysql`). `sandbox SET down` stops the containers and
removes the set's network and volumes; worktrees and the plan are kept. One repo's services
per set for now: no deduplication, loopback IPs or hosts entries yet.

```console
$ cd ~/code            # holds ./api and ./web, each with a .sandbox/ directory
$ sandbox jira-123 up --repos api web
set jira-123: /home/me/sandbox/sets/jira-123
  plan: /home/me/sandbox/sets/jira-123/sandbox.yaml (created)
  api: /home/me/sandbox/sets/jira-123/api [agent/jira-123, created from main]
  web: /home/me/sandbox/sets/jira-123/web [agent/jira-123, created from release/2.4]
  services: mysql healthy [sandbox-jira-123]
$ sandbox jira-123 down
set jira-123: sandbox-jira-123 stopped; worktrees kept in /home/me/sandbox/sets/jira-123
```

`up` renders a per-set `override.yaml` into the set directory and runs
`docker compose -p sandbox-<set> -f <repo>/.sandbox/compose.yaml -f override.yaml up -d --wait`.
The repo's own file comes first, so its relative paths resolve from the repo. The override
labels every service, network and declared volume with `sandbox.set=<set>`.

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
- **Host state is shared across processes**, so writes to it are guarded by `flock`
  (`sandbox.locking.file_lock`); `git worktree add` is serialised per source repo.
- **User-facing failures raise `SandboxError`**; the CLI prints `error: <message>` and
  exits 1 (usage errors exit 2). No tracebacks for expected failures.
