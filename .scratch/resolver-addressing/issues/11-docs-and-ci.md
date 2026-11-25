# 11: Docs and CI describe the new model

**What to build:** Someone reading the repo or its CI sees the addressing model that exists. Spec stories 77, 80.

**Blocked by:** 07, 10

**Status:** built; one criterion left for a workflow run on GitHub (see the note under it)

- [x] The README's addressing, web app, status, urls, prune, setup, configuration and development sections describe set hostnames at container addresses, the resolver, the domain and supported hosts; the hosts-file section is gone
- [x] The README states what the developer's machine can reach (every listening port, the agent included) and that Docker Desktop for Mac is unsupported
- [x] The MVP spec carries a revision note pointing at this spec and ADR 0001 for the superseded stories
- [x] The macOS release checklist is rebuilt around running the harness on a Mac with OrbStack, keeping only steps a command cannot check
- [ ] The nightly end-to-end workflow runs the harness on the Linux runner and prints its report; the per-push workflow still runs unit and integration tests
  - Not ticked: a workflow is proven only by a run on GitHub, and none was started from the session that wrote this. What was done instead: `tests/unit/test_docs.py` reads both workflow files (nightly, on demand and `v*` tags; the harness command with its opt-in on an Ubuntu runner; a report step that always runs; unit on Linux and macOS and integration on every push); the harness step's exact command line and the report step's script were run by hand in a privileged container with systemd as PID 1, a real systemd-resolved and passwordless sudo, and produced the report. That is evidence, not the criterion.
  - To tick the nightly half: `gh workflow run e2e.yml --ref claude/serene-clarke-kt4kqx`, then `gh run watch`. Expect the job `walkthrough harness (Linux, sudo, systemd-resolved)` green, the step "Walkthrough harness" ending in `1 passed`, and the run's summary page showing "sandbox walkthrough report" with a first line ending `0 failed, 0 not run`. This run also ticks the Linux half of tickets 09 and 10.
  - To tick the per-push half: `gh run list --workflow ci.yml --branch claude/serene-clarke-kt4kqx --limit 1`. Expect the run for the head commit to be green in all four jobs: `ruff + pyright (strict)`, `unit (ubuntu-latest)`, `unit (macos-latest)` and `integration (real git + Docker Compose, Linux)`.
  - What the runs on GitHub showed while this ticket was built (read, not started, from the session): the push of ticket 10 (`6fe14ec`) was red. `unit (ubuntu-latest)` hung, as the runs since ticket 05 appear to have (they are still listed as in progress): one test built its own `up` against the real machine, which on the runner has systemd-resolved and no setup, so `up` refused and the test waited for ever; one more test failed there for the same reason. `unit (macos-latest)` failed one test that read a usage error GitHub renders in colour. `integration` failed one test that parsed `/proc/1/status` too strictly for the runner's kernel; its other 91 tests passed, the Docker restart test included. All four are fixed in this ticket's second commit (tests only: no test was skipped or removed), each reproduced first in a container set up like the runner. Every CI job now has a time limit.
  - The runs of tickets 05 to 11 that hung may still be occupying runners until GitHub's six-hour limit: cancel them with `gh run list --branch claude/serene-clarke-kt4kqx --status in_progress --json databaseId -q '.[].databaseId' | xargs -n1 gh run cancel`.
  - The macOS checklist (the criterion above) was rewritten but never worked through: no Mac was available. The first person to use it on a Mac with OrbStack should correct any step that does not match what they see (`docs/release-checklist-macos.md`).
- [x] The glossary and ADR 0001 still match what was built; any term that changed is updated
