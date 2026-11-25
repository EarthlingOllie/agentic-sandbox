# 10: Harness: connect to every service

**What to build:** The harness proves that every address a set reports is usable from the developer's machine. Spec stories 62-71, 76.

**Blocked by:** 09

**Status:** built; one criterion left for a real machine and a Mac (see the note under it)

- [x] The fixture repos gain a second backing service with its own conventional port (Redis) and a service that declares no ports
- [x] For each running set the harness connects from the host to every address `urls --json` reports
- [x] Names are resolved through the operating system only; a direct query of the resolver is used for diagnostics in the report, never to pass a check
- [x] Web apps and extra hosts: an HTTPS request verified against the mkcert CA; MySQL: a login and a query; Redis: a ping; anything else: a TCP connect
- [x] Two sets run at once and the same service answers on the same conventional port in both, each returning its own set's data
- [x] The same hostnames and ports work from inside the agent
- [x] A container in one set cannot connect to the other set's service
- [x] The service with no declared ports and the agent's hostname are reachable on a port they listen on
- [x] After `up --recreate` every address is reachable again within a few seconds; after `down` the names no longer resolve; an unknown name under the domain fails fast
- [ ] It passes on a sudo-capable Linux machine with systemd-resolved, and is written to run unchanged on macOS with OrbStack
  - Not ticked: neither half was run where it says. On Linux it did pass (252 of 252 checks) in a privileged container with systemd 255 as PID 1, a real systemd-resolved, a non-root user with passwordless sudo and real Docker; that is evidence, not the criterion. On macOS nothing was run: the Linux-only gate is lifted and the harness uses nothing Linux-specific that is known, but no Mac has seen it.
  - To tick the Linux half, as a non-root user with passwordless sudo on Linux with systemd-resolved, Docker, mkcert and curl (a throwaway machine: it changes it): `uv sync --locked && SANDBOX_E2E=1 uv run pytest -m e2e tests/e2e`, or run the `E2E` workflow: `gh workflow run e2e.yml --ref claude/serene-clarke-kt4kqx`. Expect `1 passed` and a report whose summary line ends `0 failed, 0 not run`.
  - To tick the macOS half, on a Mac with OrbStack, mkcert and uv, after `sudo env "PATH=$PATH" sandbox setup`: `uv sync --locked && SANDBOX_E2E=1 SANDBOX_E2E_KEEP_SETUP=1 uv run pytest -m e2e tests/e2e` (no sudo needed in this mode; without the second switch the harness runs `sudo -n sandbox setup` itself, which needs passwordless sudo). Expect `1 passed`, `0 failed, 0 not run`.
