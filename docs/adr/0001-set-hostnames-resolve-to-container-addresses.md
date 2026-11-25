---
status: accepted
---

# Set hostnames resolve to container addresses

Sets used to give every hostname its own leased loopback IP, with the service's ports
published on it and the name written to `/etc/hosts` by a root helper. We replaced that:
nothing is published, and a shared resolver (dnsmasq forwarding the domain to Docker's own
DNS) answers each set hostname with the container's own address. Every service keeps its
conventional port in every set without sandbox allocating IPs or ports, and the privileged
surface shrinks to pointing the OS at the resolver once.

## Considered options

- **Reverse proxy on one address (nginx or Traefik).** Routes HTTPS by hostname, but plain
  TCP (MySQL, Redis) carries no hostname, so backing services would have needed a port per
  set. Rejected because conventional ports for every service was the goal.
- **Keep loopback leases beside the new model.** Would have kept Docker Desktop for Mac
  working, at the cost of the lease pool, hosts helper, `lo0` aliases and a second path to
  test.
- **Resolver records written by sandbox** instead of forwarding to Docker's DNS. Keeps the
  resolver off the sets' networks, but records go stale when a container is recreated
  outside sandbox.

## Consequences

- Docker Desktop for Mac is unsupported: its containers are not reachable at their own
  addresses. macOS needs OrbStack.
- The developer's machine can reach every port a container listens on, the agent's
  included; `ports:` in a repo's compose file is a declaration, not a gate.
- A container's address changes whenever it is recreated (`down` and `up`, `up --recreate`);
  only the set hostname is stable.
- The resolver sits on every set's network, so a container that queries it directly can
  learn other sets' hostnames. It cannot connect to them.
- Only macOS and systemd-resolved hosts are set up automatically.
