# 01: Walking skeleton — `sandbox SET up --repos` creates a set with worktrees

> **Superseded in part (agent clones):** a set's repos are no longer `git worktree`s of the source. Each is the agent's own `git clone --shared` in the set directory; the source's `.git` is mounted read-only in the agent; sandbox syncs `agent/<set>` into the source on `up`, `SET status`, `down` and `destroy`. See the revision note in `spec.md` and "The agent's clones" in the README.

**What to build:** The first end-to-end path through the tool, plus the toolchain it sits on. Running `sandbox jira-123 up --repos api web` from a directory holding `./api` and `./web` creates the set directory under the configured sets root, writes its plan (`sandbox.yaml`, pinning each repo's absolute `source` and `base`), and cuts a git worktree per repo on branch `agent/jira-123`. No containers yet. This ticket also establishes the project conventions and the primary test seam every later ticket builds on (spec: Toolchain, Testing Decisions, stories 12–16, 22–25, 27–30, 31, 35, 36, 110, 111).

**Blocked by:** None (can start immediately)

**Status:** done

- [x] Python 3.12+ project managed by uv; ruff and pyright strict pass locally and are enforced in CI; unit tests run on Linux and macOS, integration tests (real git) on Linux
- [x] Typer CLI with the `sandbox [SET] <verb>` shape; a set name that collides with a verb is rejected with a clear error
- [x] Running on Windows fails with a clear "unsupported platform" error
- [x] Global config at `~/.config/sandbox/config.yaml` (Pydantic v2) with defaults: `ip_pool` 127.42.0.0/16, templates root, sets root, default agent; invalid config fails with field-level errors
- [x] Plan schema (Pydantic v2) shared by set plans and templates: `repos.<name>.source` absolute, `base` required in a set plan; the set name is the directory name and never appears in the plan; field-level validation errors
- [x] `--repos NAME...` resolves repos from the current directory; a repo without `.sandbox/` fails before anything is created
- [x] Each worktree lives in the set directory on `agent/<set>` cut from `base`; `git worktree add` is serialised per source repo so concurrent `up` processes on the same repo don't corrupt `.git`
- [x] A later `up` for the same set name reuses a surviving `agent/<set>` branch and warns that `base` was ignored
- [x] `up` never overwrites an existing plan, and refuses when the requested repos differ from the plan on disk
- [x] Project convention recorded (e.g. in the README or contributor notes): all Docker interaction goes through the `docker` / `docker compose` CLIs via subprocess; the Python Docker SDK is not a dependency
- [x] Test seam: the CLI invoked in-process with a config pointing every host-state location at a temp dir; fakes for git available for unit runs, real git in integration runs
