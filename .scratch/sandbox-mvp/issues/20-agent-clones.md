# 20: The agent works in its own clone; sandbox syncs `agent/<set>` back

**What to build:** Close every route from the agent onto the host through git. A set's repos become the agent's own clones (`git clone --shared` into `<set dir>/<repo>`, borrowing the source's objects read-only through `alternates`) instead of `git worktree`s of the source, and the source's `.git` is mounted read-only as a whole. `up`, `sandbox SET status`, `down` and `destroy` sync the clone's `agent/<set>` into the source repo with host git that never reads the clone's config or hooks and never follows its symlinks (spec: revision note under Solution; stories 29, 30, 78, 101).

**Blocked by:** 01, 09, 14, 17

**Status:** done

- [x] The agent mounts each source `.git` read-only; it can write no config, hook, ref, log or object there, and no symlink host git (sandbox's or the developer's) would later write through, even after host git renames a new `.git/config` into place
- [x] `up` creates the clone (cut from `base`, or from a surviving `agent/<set>`) atomically, then syncs; a no-op `up` still syncs
- [x] Sync fetches the clone's `agent/<set>` into the source through `git upload-pack --strict` on a snapshot git dir sandbox writes (the branch tip, a copy of the clone's own loose objects and packs read without following symlinks or opening FIFOs/devices, and the source's objects as the only alternates), never on the clone's `.git` itself, records the tip as `refs/sandbox/agent/<set>`, fast-forwards the branch or follows the agent's rewrites when the developer hasn't moved it, and otherwise (diverged, checked out) leaves it alone and says so
- [x] A clone whose `.git` is a symlink or file is neither synced nor read; one whose `alternates` (plain or a symlink), `objects`, `refs`, `packed-refs` or `commondir` point at another host repository brings none of its objects into the source; FIFOs in the clone's git dir block nothing; host git calls time out
- [x] `SET status` reads a clone's branch and changes from a snapshot git dir sandbox writes (HEAD, branch, index read without following symlinks); it reports each repo's sync outcome
- [x] `destroy` syncs first, refuses agent commits it couldn't sync unless `--force`, removes the clones (no worktree metadata in the source to clean) and drops `refs/sandbox/agent/<set>`
- [x] Regression tests: the old config-rename and symlink routes fail from the agent and reach nothing on the host (red on 0097caf); agent commits appear as `agent/<set>` in the repo after `status` and `down`
- [x] Regression tests (review round 1): a symlinked `alternates`, symlinked `objects`/`refs`/`packed-refs`, a `commondir`, and FIFOs in the clone's git dir neither bring another repo's objects into the source nor hang `status`/`down`/`destroy`
