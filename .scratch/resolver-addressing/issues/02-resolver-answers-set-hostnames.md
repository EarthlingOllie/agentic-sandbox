# 02: The resolver answers set hostnames

**What to build:** `up` starts the shared resolver if it isn't running and attaches it to the set; `down` and a rolled-back `up` detach it. Asking the resolver directly (on its loopback port) for a set hostname returns that container's own address. The loopback model stays in place beside it, so nothing a user relies on changes yet. Spec stories 9-11, 14, 24, 25, 27, 29-31; configuration as proven by the prototype (see the spec's Implementation Decisions).

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] The resolver's image is built from a definition bundled with sandbox, tagged by content hash, and never pulled from a registry
- [ ] The resolver runs as its own Compose project, labelled as sandbox's, belonging to no set
- [ ] It forwards only the configured domain to Docker's DNS, with caching off and a 1-second answer TTL
- [ ] After `up`, a lookup of `<service>.<set>.<domain>` sent to the resolver returns the service container's address; extra hosts resolve to the web app
- [ ] Two running sets resolve side by side, each name to its own container
- [ ] After `up --recreate`, the name resolves to the new container's address within two seconds
- [ ] After `down`, and after a failed `up` has rolled back, the set's names no longer resolve and the resolver is off the set's network
- [ ] A name under the domain that no container has fails immediately (no timeout)
- [ ] `prune` and `destroy` of one set leave the resolver running and other sets resolving
- [ ] Unit tests cover attach, rollback detach and `down` detach through the CLI seam with the fake Docker; integration tests cover the lookups above against real Docker
