# 17: `sandbox SET destroy` with work-protecting safety checks

**What to build:** A developer can fully remove a set: prune it, then remove its worktrees and set directory (plan and certificate included), without ever silently losing the agent's uncommitted or unpushed work (stories 101–105).

**Blocked by:** 16 (prune)

**Status:** done

- [x] `destroy` refuses on a running set
- [x] `destroy` refuses if any worktree has uncommitted or untracked changes, naming them, unless `--force`
- [x] `agent/<set>` branches are kept by default; `--branches` deletes them but refuses on unpushed commits unless `--force` is also given
- [x] Worktrees are removed via git so the source repos' worktree metadata stays clean
- [x] On Linux, removal that hits root-owned files falls back to deleting them from a throwaway container started with the `docker` CLI (no SDK); sudo is never required
- [x] Integration tests cover each refusal and the forced paths against real git
