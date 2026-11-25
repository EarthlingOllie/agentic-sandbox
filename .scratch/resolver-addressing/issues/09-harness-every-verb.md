# 09: Harness: every verb, with a report

**What to build:** One opt-in command drives the installed `sandbox` CLI on a real machine through every verb and reports what passed. Spec stories 60, 61, 72-75.

**Blocked by:** 06, 07

**Status:** built; one criterion left for a real machine (see the note under it)

- [x] The harness runs only with the explicit end-to-end opt-in and drives the installed executable as a subprocess, with real Docker and real sudo
- [x] It runs: `setup`, `up --repos`, `save` (from repos and from a set), `templates`, `up --from`, a repeated `up`, `up --recreate`, `status`, `SET status`, `urls`, each with `--json`, `sh`, `sh --repo`, `sh SERVICE`, `claude`, `down`, `SET prune`, bare `prune`, `destroy` (a refused dirty clone, `--force`, `--branches`) and `setup --uninstall`
- [x] Each step asserts the exit code and the behaviour that verb promises, not only that it ran
- [x] `claude` is exercised without a login: the verb starts Claude Code in the agent and returns its version (the verb passes arguments through if it does not already)
- [x] The harness prints an ordered report: command, what was checked, pass or fail, and the output of anything that failed
- [x] A failed check does not stop later steps that are still meaningful, and teardown always runs
- [x] A switch keeps an existing setup in place: no `setup`, no `setup --uninstall`
- [ ] It passes on a sudo-capable Linux machine with systemd-resolved
  - Not ticked: not run on a real machine or on the GitHub runner. It did pass (142 of 142 checks) in a privileged container with systemd 255 as PID 1, a real systemd-resolved, a non-root user with passwordless sudo and real Docker; that is evidence, not the criterion. To tick it, as a non-root user with passwordless sudo on Linux with systemd-resolved, Docker, mkcert and curl: `uv sync --locked && SANDBOX_E2E=1 uv run pytest -m e2e tests/e2e`, or run the `E2E` workflow: `gh workflow run e2e.yml --ref claude/serene-clarke-kt4kqx`. Expect `1 passed` and a report with 0 failed, 0 not run.
