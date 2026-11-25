# 09: Harness: every verb, with a report

**What to build:** One opt-in command drives the installed `sandbox` CLI on a real machine through every verb and reports what passed. Spec stories 60, 61, 72-75.

**Blocked by:** 06, 07

**Status:** ready-for-agent

- [ ] The harness runs only with the explicit end-to-end opt-in and drives the installed executable as a subprocess, with real Docker and real sudo
- [ ] It runs: `setup`, `up --repos`, `save` (from repos and from a set), `templates`, `up --from`, a repeated `up`, `up --recreate`, `status`, `SET status`, `urls`, each with `--json`, `sh`, `sh --repo`, `sh SERVICE`, `claude`, `down`, `SET prune`, bare `prune`, `destroy` (a refused dirty clone, `--force`, `--branches`) and `setup --uninstall`
- [ ] Each step asserts the exit code and the behaviour that verb promises, not only that it ran
- [ ] `claude` is exercised without a login: the verb starts Claude Code in the agent and returns its version (the verb passes arguments through if it does not already)
- [ ] The harness prints an ordered report: command, what was checked, pass or fail, and the output of anything that failed
- [ ] A failed check does not stop later steps that are still meaningful, and teardown always runs
- [ ] A switch keeps an existing setup in place: no `setup`, no `setup --uninstall`
- [ ] It passes on a sudo-capable Linux machine with systemd-resolved
