# 11: Agent overrides that can't remove the safety guarantees

**What to build:** A developer can bring their own agent image or add sidecars (e.g. an egress proxy) with a Compose fragment in the global config, a template, or a set plan. Sandbox merges it over the default agent, then re-applies its own guarantees, and validates a custom image before any container starts (stories 83–87).

**Blocked by:** 02 (templates), 09 (agent container and `sh`)

**Status:** done

- [x] An `agent` Compose fragment is accepted in config, templates and set plans, with the set plan winning, then template, then config; it is pinned into the set plan like `source`
- [x] After merging, sandbox re-applies mounts, user, CA mount, `CLAUDE_CONFIG_DIR`, capability drops and the no-Docker-socket rule; an override that tries to mount the socket or run as root is overridden (verified by inspecting the rendered override)
- [x] Extra services in the fragment (e.g. a proxy) join the set network
- [x] `up` checks a custom image has `sh`, `git` and `claude` on PATH (by running the image with the `docker` CLI, no SDK) and fails before any other container starts if not
- [x] Docs state egress is unrestricted in the MVP and show a proxy override example

Note: sets have no CA until ticket 12 (HTTPS web apps); the re-applied mounts are built in one place (`_guarantees` in `src/sandbox/compose/agent.py`), where 12 adds the `rootCA.pem` mount so overrides cannot remove it either.
