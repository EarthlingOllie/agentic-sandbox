# 08: The resolver is hardened and safe under concurrency

**What to build:** The one component every set's containers can send packets to is locked down, and it behaves when several sets come up at once or Docker restarts. Spec stories 12, 26, 28, 32-35.

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] The resolver runs with capabilities dropped except what binding its port needs, no-new-privileges, a read-only root filesystem, no Docker socket and no mounts beyond its config
- [ ] It listens on the machine's loopback only, on a fixed port that is not 53
- [ ] It refuses names outside the domain and never forwards a name under the domain to an upstream DNS server
- [ ] Several `up`s started at the same moment leave exactly one resolver running, attached to every one of those sets
- [ ] After a restart of the resolver or of Docker, running sets resolve again with no sandbox command
- [ ] The README states that a container querying the resolver directly can learn other sets' hostnames but cannot connect to them
- [ ] Integration tests cover the concurrent start, the restart, the refusal of outside names and that a container in one set cannot connect to another set's service
