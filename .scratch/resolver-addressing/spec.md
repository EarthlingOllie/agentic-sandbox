# Spec: set hostnames resolve to container addresses (the resolver)

Labels (when published): `ready-for-agent`
Decision record: ADR 0001 (set hostnames resolve to container addresses). Vocabulary: `CONTEXT.md`.
Supersedes, in the MVP spec: stories 2-4, 6, 10, 13, 52-53, 56-61, and the Addressing, IpPool and HostsFile parts of its Implementation Decisions.

## Problem Statement

I like what `sandbox` does: one command gives an agent a set, and I can run several sets side by side. What I don't like is how a set's services reach my machine.

- Every set hostname is given its own leased loopback IP, and each service's ports are published on it. Sandbox has to run an IP lease pool to make that work, and leases leak when something crashes.
- Names come from `/etc/hosts`, so sandbox ships a root-owned helper and a sudoers rule just to edit that file on every `up` and `down`.
- On macOS only 127.0.0.1 answers on loopback, so setup installs a launchd job that aliases thousands of addresses at boot, and the number of sets I can run is capped by how many I asked for.
- All of that is privileged, platform-specific machinery that exists only to work around several containers wanting the same port.

I want services and web apps reachable from my machine by name, each on its conventional port, in every set at once, without sandbox allocating IPs or ports and with as little root as possible. And I want to be able to prove, on any machine, that every command works and every service really is reachable.

## Solution

Nothing is published on the host any more. Every container already has its own address, so every service in every set keeps its conventional port: `mysql.jira-123.test:3306` and `mysql.jira-456.test:3306` both work at once.

A single shared **resolver** (dnsmasq, in a container that belongs to no set) answers lookups of set hostnames from my machine by forwarding them to Docker's own DNS. It carries no traffic: my client asks the resolver for the name, then connects straight to the container. There is no reverse proxy.

`sudo sandbox setup [--domain X]` does the only privileged work, once: it points my operating system at the resolver for the **domain** (`test` unless I say otherwise) and installs the mkcert CA. After that `up` and `down` need no root at all. The set hostname is the stable address; container addresses change between `up`s and I never need to know them.

This works on **supported hosts**: Linux, and macOS with OrbStack. Docker Desktop for Mac cannot reach containers at their own addresses, so `up` refuses there and says why.

A **walkthrough harness** drives the installed CLI through every command and connects to every set hostname from the host, so "it all works" is something I run rather than something I trust.

## User Stories

### Addressing

1. As a developer, I want every service in a set reachable from my machine at `<service>.<set>.<domain>` on its conventional port, so that my database client, browser and scripts use the same address the committed config implies.
2. As a developer, I want two sets that both run `mysql` to both answer on 3306 at the same time, so that parallel sets never collide.
3. As a developer, I want that to hold for every service and every port, not only HTTPS, so that there is one rule to remember.
4. As a developer, I want sandbox to allocate no IPs and no ports, so that there is no lease state to leak or exhaust.
5. As a developer, I want the number of sets I can run to be limited only by Docker, so that I never rerun setup to make room.
6. As a developer, I want the same set hostname and port to work from my machine and from inside the set, so that tools and humans share one address.
7. As a developer, I want services inside a set to keep reaching each other by short name (`mysql`), so that committed app config still works unchanged.
8. As a developer, I want the set hostname to be the stable address across `down` and `up`, so that bookmarks and tool configs survive even though the container's address changes.
9. As a developer, I want a recreated service to be reachable at its new address by name within a couple of seconds, so that I never connect to a dead address.
10. As a developer, I want a set hostname to stop resolving once its set is down, so that a stale name never points at another container.
11. As a developer, I want an unknown name under the domain to fail immediately, so that a typo doesn't hang my client.
12. As a developer, I want lookups under the domain never forwarded to my upstream DNS, so that set and service names don't leave my machine.
13. As a developer, I want the agent to answer at `agent.<set>.<domain>`, so that I can open something the agent is serving without looking up an address.
14. As a developer, I want a web app's extra hosts to resolve to the web app, so that multi-host apps keep working.
15. As a repo maintainer, I want any host-side port binding in my compose file ignored, so that my `0.0.0.0:3306` neither collides between sets nor opens a port to the network.
16. As a repo maintainer, I want `ports:` to remain the way I declare what my service offers, so that `sandbox SET urls` lists the right addresses.
17. As a developer, I want it documented that my machine can reach every port a container listens on, declared or not and the agent's included, so that I am not surprised by what is reachable.
18. As a security-conscious developer, I want a container in one set still unable to connect to another set's containers, so that sets stay isolated.

### Web apps

19. As a repo maintainer, I want the web app contract unchanged (`x-sandbox: { web: true }`, the app listening on 443 with the set's certificate mounted), so that my repos need no edits.
20. As a developer, I want `https://<repo>.<set>.<domain>` to open in my browser with a trusted certificate, so that cookies, CORS and OAuth callbacks behave.
21. As a developer, I want the set's wildcard certificate issued for the configured domain, so that a non-default domain still gets valid HTTPS.
22. As a front-end developer, I want `vite: <port>` to keep setting the HMR host, reachable on that port by the web app's hostname, so that hot reload works with nothing published.
23. As a developer, I want no reverse proxy between me and a web app, so that what I see is exactly what the app serves.

### The resolver

24. As a developer, I want `up` to start the resolver if it isn't running, so that I never manage it by hand.
25. As a developer, I want `up` to attach the resolver to the set and `down` to detach it, so that only running sets resolve.
26. As a developer, I want the resolver to come back with Docker after a reboot, so that running sets resolve without another command.
27. As a developer, I want the resolver shared by all sets and owned by none, so that bringing one set down never breaks another's names.
28. As a developer, I want concurrent `up`s to start exactly one resolver, so that parallel sets are safe.
29. As a developer, I want a rolled-back `up` to detach the resolver from that set, so that a failed set leaves nothing behind.
30. As a developer, I want `prune` and `destroy` to leave the resolver running for other sets, so that cleanup of one set is local.
31. As a developer, I want the resolver built from a definition that ships with sandbox, never pulled from a third-party registry, so that I know what answers my DNS.
32. As a security-conscious developer, I want the resolver hardened (non-root where possible, capabilities dropped, read-only, no Docker socket), so that a component every agent can send packets to is hard to abuse.
33. As a security-conscious developer, I want it documented that a container which deliberately queries the resolver can learn other sets' hostnames but cannot connect to them, so that I know the limit of the isolation.
34. As a developer, I want the resolver to listen on my machine's loopback only, on a port that isn't 53, so that it neither serves my network nor clashes with another local DNS server.
35. As a developer, I want the resolver to answer only names under the domain, so that it can't be mistaken for a general DNS server.

### Setup and prerequisites

36. As a developer, I want `sudo sandbox setup` to point my operating system at the resolver for the domain, so that set hostnames resolve in every program.
37. As a developer, I want `sudo sandbox setup --domain X` to choose the domain and record it in my config, so that there is one place the value lives.
38. As a developer, I want the domain to default to `test` when I omit `--domain`, so that the common case needs no flag.
39. As a developer, I want rerunning setup with a different `--domain` to switch over cleanly, so that I can move away from a suffix another tool claims.
40. As a developer, I want `local` rejected as a domain, so that I don't fight mDNS.
41. As a macOS developer, I want setup to use the per-domain resolver configuration macOS provides, so that only names under the domain go to the resolver.
42. As a Linux developer on systemd-resolved, I want setup to install a per-domain drop-in, so that only names under the domain go to the resolver.
43. As a Linux developer without systemd-resolved, I want setup to refuse and print the one line to add by hand, so that I am not left with a half-configured machine.
44. As a developer, I want setup to keep installing the mkcert CA as me, never as root, so that my browser trusts set certificates.
45. As a developer, I want setup never to start containers, so that the privileged step stays small.
46. As a developer upgrading, I want setup to remove what the loopback model installed (the hosts helper, its sudoers rule, the macOS alias job and its aliases, leftover `# sandbox:` hosts blocks), so that nothing privileged lingers.
47. As a developer, I want `sudo sandbox setup --uninstall` to remove the resolver configuration and the resolver itself and leave the mkcert CA, so that I can remove sandbox cleanly.
48. As a developer, I want `up` to check, before creating anything, that my machine sends the domain to the resolver, and to tell me to run setup if not, so that I never get a set I can't reach.
49. As a developer, I want `up` to notice that the configured domain differs from what setup installed and tell me to rerun setup, so that the two can't drift silently.
50. As a macOS developer on Docker Desktop, I want `up` to refuse with an explanation that names OrbStack, so that I don't debug unreachable hostnames.
51. As a macOS developer on OrbStack with direct container access switched off, I want `up` to name that setting, so that I know what to turn on.
52. As a developer, I want sets without services to need none of this, so that clone-only sets stay trivial.
53. As a developer upgrading, I want a set created under the loopback model to work after `down` and `up`, with no migration step, so that upgrading is cheap.

### Inspecting sets

54. As a developer, I want `sandbox SET status` to show each service's set hostname, and its current address while the set runs, so that I can see where things are.
55. As a developer, I want a stopped set to show its hostnames without addresses, so that I am not shown an address that no longer exists.
56. As a developer, I want `sandbox SET urls` to list web apps as `https://` URLs and other services as `host:port` with their conventional ports, so that I can copy an address.
57. As a developer, I want `urls` to include the agent's hostname, so that I can find it.
58. As a script author, I want `--json` on `status` and `urls` to stay the same models the tables render, with the address optional, so that tooling keeps working.
59. As a developer, I want `sandbox status` to stop reporting IP leases as orphans, so that the orphan list reflects only what still exists.

### The walkthrough harness

60. As a developer, I want one command that runs the installed CLI through every verb, so that I can prove a build works on a real machine.
61. As a developer, I want the harness to run `setup`, `up --repos`, `up --from`, a repeated `up`, `up --recreate`, `status`, `SET status`, `urls`, `sh`, `sh --repo`, `sh SERVICE`, `claude`, both forms of `save`, `templates`, `down`, `SET prune`, bare `prune`, `destroy` (its refusals, `--force` and `--branches`) and `setup --uninstall`, so that no verb is left untested.
62. As a developer, I want the harness to connect from the host to every address `urls --json` reports, so that the list of addresses and reality can't disagree.
63. As a developer, I want the harness to speak the real protocol where it can (an HTTPS request verified against the mkcert CA for web apps, a MySQL login and query, a Redis ping), so that "reachable" means usable and not merely an open port.
64. As a developer, I want the harness to resolve names the way any program on my machine does, through the operating system, so that it tests my setup and not a shortcut.
65. As a developer, I want the harness to run two sets at once and check the same service on the same conventional port in both, so that the central promise is tested directly.
66. As a developer, I want the harness to check that each set reaches its own instance (different data in each), so that names never cross sets.
67. As a developer, I want the harness to check the same hostnames from inside the agent, so that inside and outside agree.
68. As a developer, I want the harness to check that a container in one set cannot connect to the other set's service, so that isolation is proven.
69. As a developer, I want the harness to recreate a set and check that names follow the new containers, so that staleness is caught.
70. As a developer, I want the harness to check that names stop resolving after `down` and that an unknown name fails fast, so that the negative cases are covered.
71. As a developer, I want the harness to check a service that declares no ports and the agent's hostname, so that the widened reachability is what the docs say.
72. As a developer, I want the harness to finish with a per-step report (command, what was checked, pass or fail, and the output of anything that failed), so that I can see exactly what broke.
73. As a developer, I want the harness to keep going after a failed check where later steps are still meaningful, and always tear down what it created, so that one failure doesn't hide others or leave sets behind.
74. As a developer, I want the harness to refuse to run unless I opt in explicitly, so that it never changes a machine by accident.
75. As a developer on an already set-up machine, I want a mode that skips `setup` and `setup --uninstall`, so that I can run the harness without disturbing my installation.
76. As a macOS developer, I want to run the same harness on a Mac with OrbStack, so that the macOS path is verified by a command rather than a checklist.
77. As a maintainer, I want the harness to run on the sudo-capable Linux runner nightly, on demand and for release tags, so that regressions are caught without a person.

### Engineering quality

78. As a contributor, I want the loopback machinery deleted rather than left dormant (lease pool, hosts block and helper, alias setup, port rewriting, their config keys and tests), so that the codebase has one addressing model.
79. As a contributor, I want unit tests to keep running on Linux and macOS with no Docker and no root, so that the fast loop stays fast.
80. As a contributor, I want the README, the MVP spec and the macOS release checklist to describe the new model, so that docs and behaviour agree.

## Implementation Decisions

### Addressing

- A set hostname is `<name>.<set>.<domain>`. Each service keeps its short name and its set hostname (plus a web app's extra hosts) as names on the set's network, as today; the agent gains `agent.<set>.<domain>`.
- The network part of the per-set override stops publishing ports. It resets every repo-declared host-side binding and publishes nothing. The container-side ports a service declares are still parsed; they are what `urls` reports.
- The lease step leaves the `up` pipeline and the hosts-block step is replaced by attaching the resolver. No per-set addressing state is written anywhere.
- The uniqueness check on hostnames within a set stays (two things in one set may not claim one name).

### The resolver

- One container, in its own Compose project, labelled as sandbox's and belonging to no set. Its image is built from a definition bundled with sandbox, tagged by content hash, like the default agent image.
- Configuration, as proven by the prototype (Docker 29, Linux):

  ```
  no-resolv
  no-hosts
  cache-size=0
  max-ttl=1
  bind-dynamic
  except-interface=lo
  server=/<domain>/127.0.0.11
  ```

  The container is started with an unusable upstream DNS server so that Docker's DNS fails unknown names immediately instead of forwarding them. Caching is off and the answer TTL is 1 second because Docker answers with a 600-second TTL and a cached answer outlives a recreated container.
- It publishes DNS (UDP and TCP) on the machine's loopback only, on a fixed non-53 port. The network that publication goes through is the resolver's own and stays attached for its lifetime; set networks are attached and detached around it.
- A new provisioning step, after the set's services are up and its hooks have run: ensure the resolver is running (serialised across processes with the existing file lock) and attach it to the set's network. Its rollback detaches. `down` detaches before removing the network. Attaching and detaching are idempotent.
- The resolver restarts with Docker. Because Docker restores a container's networks on restart, running sets resolve again without sandbox.
- Hardening: all capabilities dropped except what binding its port needs, no-new-privileges, read-only root filesystem, no Docker socket, no mounts beyond its config.
- Resolver operations go through the existing Docker and Compose collaborators (CLI only, no SDK); attach and detach are new operations on the Docker collaborator.

### Setup

- `setup` gains `--domain`; `--sets` is removed. Setup writes `domain` into the invoking user's config (as that user) and installs the operating system's per-domain resolver configuration naming the resolver's loopback address and port: the per-domain resolver file on macOS, a systemd-resolved drop-in followed by a reload on Linux.
- Setup detects systemd-resolved; without it, it refuses before installing anything and prints the manual line.
- Setup removes loopback-model leftovers when present. `--uninstall` removes the resolver configuration (for whichever domain was installed), stops and removes the resolver container, and prints the `mkcert -uninstall` hint as today.
- Setup records which domain it installed so that `up` can compare it with the config.

### Prerequisite checks in `up`

- For a set with services, before anything is created: the per-domain resolver configuration exists and matches the configured domain; on macOS the Docker runtime is OrbStack with direct container access. A set with a web app still requires the mkcert CA.
- The checks for the hosts helper, its sudoers rule and the alias pool are removed.

### Configuration

- New key `domain` (default `test`; a valid DNS suffix of one or more labels; `local` rejected). Removed keys: `ip_pool`, `hosts_file`. A config that still carries a removed key fails with a message naming the key and saying it is no longer used.

### Status and urls

- A running service's address is read from Docker when the command runs. The output models make the address optional; it is absent for stopped sets. `urls` adds the agent.
- Orphan detection reconciles set directories against labelled containers only; the lease file is no longer read.

### Removed modules

- The IP lease pool, the hosts block writer and its root helper, the loopback alias setup and its launchd job, and the bindings value object's address-allocation role.

### The walkthrough harness

- The existing end-to-end suite becomes the harness: it drives the installed `sandbox` executable as a subprocess, with real Docker, real name resolution and real sudo.
- Connection checks are driven by `urls --json`, so every reported address is exercised; protocol-specific probes are chosen by service name for the fixture services, with a plain TCP connect as the fallback.
- Name resolution in the harness uses the operating system resolver only. Querying the resolver directly is used for diagnostics when an OS lookup fails, never as a substitute.
- The harness keeps an ordered step log and prints it as a report at the end; teardown (`down`, `destroy`, and `setup --uninstall` unless skipped) runs regardless of failures.
- It runs only with the existing explicit opt-in. A second switch keeps an existing setup in place (no `setup`, no `--uninstall`).
- `claude` is exercised as far as it can be without a login: the verb starts Claude Code in the agent and returns its version. If the verb cannot pass arguments through today, it gains that.
- Fixtures grow a second backing service with its own conventional port (Redis) and a service that declares no ports, alongside the existing shared MySQL, init hook and web app with an extra host.

## Testing Decisions

### What makes a good test

Tests exercise external behaviour through public seams: exit codes and output of the CLI, the files sandbox writes (plan, override, config, resolver configuration), and the observable state of Docker and of name resolution. They do not assert on private functions, call order or internal structures.

### Seams (to confirm)

No new seam is introduced.

1. **The CLI, in-process** (existing primary seam): the app invoked with injected collaborators. Unit runs use the existing fakes; the fake Docker learns the resolver's operations (ensure running, attach, detach, container address) and the fake host learns the per-domain resolver configuration, so setup and prerequisite checks are tested without root. Integration runs pass real git and Docker through the same seam.
2. **The installed CLI on a real machine** (existing end-to-end seam): this is the walkthrough harness.

The dependency-resolver seam is untouched by this work.

### Layers

| Layer | Covers | Runs on |
|---|---|---|
| Unit | override publishes nothing and resets repo bindings; agent hostname; `domain` validation and removed keys; setup on macOS, systemd-resolved and unsupported Linux, including `--domain` changes, leftover cleanup and `--uninstall`; prerequisite failures and their messages; resolver attach, rollback detach and `down` detach; `status`, `urls` and their JSON with and without addresses | every push, Linux and macOS |
| Integration | real resolver with real sets: names resolve when the resolver is asked directly, two sets on one conventional port, recreate follows the new address, detach on `down`, unknown name fails fast, concurrent `up`s start one resolver | every push, Linux |
| End to end (the harness) | every verb; every address from `urls --json` through the operating system resolver, by protocol; both sets at once; from the agent; isolation between sets | Linux runner with passwordless sudo, nightly, on demand and for tags; a person on macOS with OrbStack before each release |

### Prior art

The existing unit tests for setup and prerequisite checks (fake host and fake runner), the integration tests for the network and web fragments, and the existing end-to-end lifecycle test, which the harness extends.

## Out of Scope

- Docker Desktop for Mac, and any second addressing backend.
- A reverse proxy, central TLS termination, or any change to the web app contract.
- Automatic resolver configuration on Linux without systemd-resolved.
- Preventing a container from learning other sets' hostnames through the resolver.
- Restricting which container ports the developer's machine can reach.
- Migrating sets in place; they are brought `down` and `up` again.
- Network egress restriction for the agent (unchanged from the MVP).
- Windows.

## Further Notes

- The prototype (two networks, one dnsmasq container, Docker 29 on Linux) established: lookups from the host return each container's address, including for a network attached while dnsmasq runs; the host connects to a container's conventional port with nothing published; several names on one container all resolve; a container in one set cannot connect to another set's container; with dnsmasq's default cache a recreated container resolves to its dead address, and with the cache off it does not.
- Not yet verified anywhere: macOS with OrbStack end to end, and whether a host stub resolver (systemd-resolved, macOS) honours the 1-second TTL after a recreate. The harness covers both; story 9 is the acceptance check.
- The OrbStack facts (containers reachable by address from the Mac; a setting that enables it; its own domain feature limited to `.local`) come from its documentation and have not been exercised by this project.
- Docker Hub rate limits were hit during the prototype; building the resolver image from a bundled definition avoids a pull at `up`.
