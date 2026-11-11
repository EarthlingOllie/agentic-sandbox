# 03: Backing services come up healthy under Compose; `down` stops them

> **Superseded in part (agent clones):** a set's repos are no longer `git worktree`s of the source. Each is the agent's own `git clone --shared` in the set directory; the source's `.git` is mounted read-only in the agent; sandbox syncs `agent/<set>` into the source on `up`, `SET status`, `down` and `destroy`. See the revision note in `spec.md` and "The agent's clones" in the README.

**What to build:** A set whose repo declares a backing service in `.sandbox/compose.yaml` gets that service started as Compose project `sandbox-<set>`, reachable by its short alias on the set's network, and `up` blocks until healthchecks pass. `sandbox SET down` stops containers and removes the network and volumes while keeping worktrees. Single repo only; no deduplication, IPs or hosts yet (stories 33, 37, 44, 54, 97, 100).

**Blocked by:** 01 (walking skeleton)

**Status:** done

- [x] Compose is driven through its CLI via subprocess (no Docker SDK); unit runs use a fake behind the CLI seam, integration runs use real Docker
- [x] A per-set override file is rendered into the set directory; the repo's original compose file is passed alongside it so relative paths resolve from the repo
- [x] Every resource is labelled with the set name so it can later be reconciled
- [x] `up` runs `compose up -d --wait` and returns only when every service is healthy
- [x] A repo with `.sandbox/` but no `compose.yaml` still provisions (worktree only)
- [x] `down` stops containers and removes network and volumes; worktrees are untouched
- [x] Fixture repo with a `mysql` service used by the integration test
