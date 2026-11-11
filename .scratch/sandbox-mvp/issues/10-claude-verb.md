# 10: `sandbox SET claude` launches Claude Code in the agent container

**What to build:** A developer starts the agent for a set with one command. All sets share one Claude login through the shared config mount; on macOS the first run performs `/login` inside the container, and every set is logged in thereafter (stories 73, 79, 80).

**Blocked by:** 09 (agent container and `sh`)

**Status:** done

- [x] `sandbox SET claude` runs Claude Code interactively in the agent container, starting in `/workspace/<set>/`
- [x] Fails with a clear message if the set isn't running
- [ ] When no credentials exist in the shared config, the first run leads the user through `/login`, which writes `.credentials.json` (0600) into the shared mount; a second set then starts already logged in — *needs manual verification with a real Claude account (implemented; covered by fakes in `tests/unit/test_claude.py`)*
- [x] Docs record the accepted trade-offs: plaintext token on macOS, occasional forced re-login on concurrent refresh
