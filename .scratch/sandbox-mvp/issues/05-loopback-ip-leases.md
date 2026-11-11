# 05: Sticky loopback IP leases with native ports and `.test` aliases

**What to build:** Every hostname in a set gets its own loopback IP from a /24 leased to the set, and keeps its native port, so `mysql.jira-123.test:3306` and `mysql.jira-456.test:3306` coexist. Services are also reachable inside the set at `<service>.<set>.test`. `up` prints the set's bindings (stories 34, 52–57, 61).

**Blocked by:** 03 (Compose single repo)

**Status:** done

- [x] Each set leases one /24 from the configured pool (default 127.42.0.0/16); each hostname takes an IP in .1–.254
- [x] The lease file is `flock`-guarded; concurrent `up` processes never hand out the same block or IP
- [x] Leases are sticky: `down` then `up` yields the same IPs; leases are released only by `prune`
- [x] Ports are published only on the hostname's IP (e.g. `127.42.3.5:3306:3306`); two sets with the same service come up side by side on Linux
- [x] Each service has both its short alias and `<service>.<set>.test` as network aliases on the set network
- [x] `up` prints each binding (hostname, IP, port) on success and on the no-op path
