# 07: Multi-repo sets share deduplicated services, failing on conflicting definitions

**What to build:** A set of several repos that both declare `mysql` gets exactly one `mysql`. Same-named services whose definitions disagree fail provisioning, before any container starts, with a diff naming both repos (stories 38–43, 45; spec: Deduplication rule). This is the secondary test seam: repo compose files in → merged service graph or conflict diff out.

**Blocked by:** 03 (Compose single repo)

**Status:** done

- [x] Each repo's compose file is normalised on its own with `docker compose config` before comparison, so `extends` etc. don't cause false conflicts
- [x] Services match by name only (never by image); identical normalised definitions merge into one service
- [x] `depends_on`, `labels`, `ports` and `x-sandbox` are ignored in the comparison
- [x] `healthcheck` set by only one repo is kept; two differing healthchecks conflict
- [x] Any other difference fails with a readable diff naming both repos
- [x] All repos' original compose files plus the override are passed to Compose so relative paths still resolve
- [x] Fixture: two repos sharing `mysql`, plus a conflicting variant; the resolver's rule combinations are unit-tested at its own seam, one multi-repo `up` is covered through the CLI
- [x] Docs note that multi-version services (e.g. `mysql84` beside `mysql`) are unsupported in the MVP
