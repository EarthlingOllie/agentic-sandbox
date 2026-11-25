# Sandbox

Provisioning isolated environments in which coding agents work on several repositories at once.

## Language

### Sets

**Set**:
An isolated environment for one unit of work, named after it (e.g. `jira-123`): the agent's clones, the services those repos need, and an agent.
_Avoid_: Environment, sandbox (that is the tool), stack

**Plan**:
The definition of a set: which repos take part and the branch each clone is cut from.
_Avoid_: Manifest, spec

**Template**:
A reusable plan that is not yet tied to a set.

**Clone**:
The agent's own copy of a repo inside a set, on branch `agent/<set>`.
_Avoid_: Worktree, checkout

**Sync**:
Bringing a clone's `agent/<set>` branch into the developer's repo.
_Avoid_: Push, pull

**Agent**:
The hardened container of a set in which the coding agent runs.

### Services

**Service**:
A container a repo declares for a set; repos that declare the same name share one.
_Avoid_: Dependency, backing service (a web app is a service too)

**Web app**:
A service that serves HTTPS under its repo's name.
_Avoid_: App, site, frontend

**Extra host**:
A further set hostname that leads to a web app.
_Avoid_: Alias, subdomain

### Addressing

**Domain**:
The DNS suffix every set hostname ends in, chosen at setup (`test` by default).
_Avoid_: TLD, suffix

**Set hostname**:
The name a service, extra host or agent answers to, `<name>.<set>.<domain>`; it is the same from the developer's machine and from inside the set, and it is the stable address.
_Avoid_: URL, binding, address

**Short name**:
The bare service name (`mysql`) by which members of one set reach each other.
_Avoid_: Alias

**Conventional port**:
The port a service listens on in its own image (3306 for MySQL); every set uses it unchanged.
_Avoid_: Native port, mapped port

**Resolver**:
The one shared component, belonging to no set, that answers lookups of set hostnames from the developer's machine. It carries no traffic.
_Avoid_: Proxy, DNS server, gateway

**Supported host**:
A machine from which containers are reachable at their own addresses: Linux, or macOS with OrbStack.

### Verification

**Walkthrough harness**:
The opt-in run that drives the installed `sandbox` through every verb on a real machine, connects to every address each set reports, and ends with a report.
_Avoid_: E2E suite, lifecycle test, smoke test
