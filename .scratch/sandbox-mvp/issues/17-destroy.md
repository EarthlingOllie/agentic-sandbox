# 17: `sandbox SET destroy` with work-protecting safety checks

> **Superseded in part (agent clones):** a set's repos are no longer `git worktree`s of the source. Each is the agent's own `git clone --shared` in the set directory; the source's `.git` is mounted read-only in the agent; sandbox syncs `agent/<set>` into the source on `up`, `SET status`, `down` and `destroy`. See the revision note in `spec.md` and "The agent's clones" in the README.

**What to build:** A developer can fully remove a set: prune it, then remove its worktrees and set directory (plan and certificate included), without ever silently losing the agent's uncommitted or unpushed work (stories 101–105).

**Blocked by:** 16 (prune)

**Status:** done

- [x] `destroy` refuses on a running set
- [x] `destroy` refuses if any worktree has uncommitted or untracked changes, naming them, unless `--force`
- [x] `agent/<set>` branches are kept by default; `--branches` deletes them but refuses on unpushed commits unless `--force` is also given
- [x] Worktrees are removed via git so the source repos' worktree metadata stays clean
- [x] On Linux, removal that hits root-owned files falls back to deleting them from a throwaway container started with the `docker` CLI (no SDK); sudo is never required
- [x] Integration tests cover each refusal and the forced paths against real git
