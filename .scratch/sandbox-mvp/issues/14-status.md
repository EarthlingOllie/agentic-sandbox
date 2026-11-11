# 14: `sandbox status` and `sandbox SET status`, with `--json`

**What to build:** A developer can see every set at a glance with its state (running, stopped, partial, orphaned), and drill into one set to see each service's health, hostnames and IPs, and each repo's branch with a marker for uncommitted work. Script authors get the same models as JSON (stories 88, 89, 91; spec: set states, orphan reconciliation).

**Blocked by:** 04 (rollback and state table), 05 (loopback IP leases)

**Status:** done

- [x] State is derived by reconciling set directories and the lease file against Docker labels (queried with the `docker` CLI, no SDK): running, stopped, partial, orphaned (leftover containers/leases with no set directory, or vice versa)
- [x] `sandbox status` lists all sets with state
- [x] `sandbox SET status` shows services with health, hostnames and IPs, and repos with branch and a dirty/untracked marker
- [x] Output models are Pydantic models shared by the table renderer and `--json`
