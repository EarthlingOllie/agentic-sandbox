# 19: End-to-end suite on a sudo-capable Linux runner

**What to build:** A CI job that exercises the whole product as a user would, including the privileged parts unit and integration tests can't reach: `sudo sandbox setup`, `up` of the fixture repos, HTTPS from the host and from the agent, `down`, and `destroy` (story 111; spec: Testing Decisions, Layers).

**Blocked by:** 13 (extra hosts and Vite), 17 (destroy), 18 (setup)

**Status:** done

- [x] Runs on a Linux runner with passwordless sudo; separate from the per-push unit/integration jobs
- [x] Covers setup → `up` (two repos sharing `mysql`, a web app with an extra host and an init hook) → HTTPS via real `/etc/hosts` from host and agent → `down` → `destroy` → `setup --uninstall`
- [x] Two sets provisioned in parallel both succeed with no port collisions
- [x] A documented manual macOS checklist to run before each release (`docs/release-checklist-macos.md`)

Needs manual verification on macOS: the checklist itself (launchd `lo0` alias pool across a reboot, keychain/browser trust of the mkcert CA, Docker Desktop file ownership, pool exhaustion with `--sets`, `claude` `/login`) has not been run on a Mac yet; a person runs it before the first release. The Linux suite was run locally in a throwaway Ubuntu 24.04 container acting as a sudo-capable runner (host network, host Docker socket); the first green run of `.github/workflows/e2e.yml` on a GitHub-hosted runner is still to come.
