# 08: Per-repo init hooks run inside shared services

**What to build:** A repo can ship `.sandbox/init/<service>.sh` to create its own database, users or seed data in a (possibly shared) service. Once the service is healthy, sandbox runs every repo's hook for it, in repo-name order, on every `up`; a failing hook fails and rolls back the set (stories 46–51).

**Blocked by:** 04 (rollback and state table), 07 (service deduplication)

**Status:** done

- [x] Each hook is mounted read-only at `/sandbox/init/<repo>/` in the target service and executed with `docker compose exec` after the service is healthy
- [x] With two repos hooking the same service, hooks run in repo-name order
- [x] Hooks run on every `up`, including a resume
- [x] A non-zero hook exit fails the set and triggers rollback, with the hook's output surfaced
- [x] Init-hook volumes never count as a dedup conflict
- [x] Fixture repo's init hook creates a database, verified in an integration test
