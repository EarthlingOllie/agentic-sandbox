# 11: Docs and CI describe the new model

**What to build:** Someone reading the repo or its CI sees the addressing model that exists. Spec stories 77, 80.

**Blocked by:** 07, 10

**Status:** ready-for-agent

- [ ] The README's addressing, web app, status, urls, prune, setup, configuration and development sections describe set hostnames at container addresses, the resolver, the domain and supported hosts; the hosts-file section is gone
- [ ] The README states what the developer's machine can reach (every listening port, the agent included) and that Docker Desktop for Mac is unsupported
- [ ] The MVP spec carries a revision note pointing at this spec and ADR 0001 for the superseded stories
- [ ] The macOS release checklist is rebuilt around running the harness on a Mac with OrbStack, keeping only steps a command cannot check
- [ ] The nightly end-to-end workflow runs the harness on the Linux runner and prints its report; the per-push workflow still runs unit and integration tests
- [ ] The glossary and ADR 0001 still match what was built; any term that changed is updated
