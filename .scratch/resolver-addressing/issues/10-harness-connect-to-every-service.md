# 10: Harness: connect to every service

**What to build:** The harness proves that every address a set reports is usable from the developer's machine. Spec stories 62-71, 76.

**Blocked by:** 09

**Status:** ready-for-agent

- [ ] The fixture repos gain a second backing service with its own conventional port (Redis) and a service that declares no ports
- [ ] For each running set the harness connects from the host to every address `urls --json` reports
- [ ] Names are resolved through the operating system only; a direct query of the resolver is used for diagnostics in the report, never to pass a check
- [ ] Web apps and extra hosts: an HTTPS request verified against the mkcert CA; MySQL: a login and a query; Redis: a ping; anything else: a TCP connect
- [ ] Two sets run at once and the same service answers on the same conventional port in both, each returning its own set's data
- [ ] The same hostnames and ports work from inside the agent
- [ ] A container in one set cannot connect to the other set's service
- [ ] The service with no declared ports and the agent's hostname are reachable on a port they listen on
- [ ] After `up --recreate` every address is reachable again within a few seconds; after `down` the names no longer resolve; an unknown name under the domain fails fast
- [ ] It passes on a sudo-capable Linux machine with systemd-resolved, and is written to run unchanged on macOS with OrbStack
