# 16: `sandbox SET prune` and bare `sandbox prune`

**What to build:** A developer can clear a stopped set's container infrastructure and release its IP leases back to the pool, or clear every orphaned environment left by crashes in one command. Worktrees are never touched (stories 98–100, 103; spec: Concurrency note on multi-set operations).

**Blocked by:** 06 (hosts file block), 14 (status)

**Status:** done

- [x] `sandbox SET prune` removes the set's containers, network, volumes and hosts block, and releases its IP leases; worktrees and plan remain
- [x] `prune` refuses on a running set
- [x] Bare `sandbox prune` finds every orphan via the status reconciliation and clears each, sets processed concurrently under a bound and independently (one failure doesn't stop the rest)
- [x] After pruning, a fresh `up` of the same set gets a newly leased block
