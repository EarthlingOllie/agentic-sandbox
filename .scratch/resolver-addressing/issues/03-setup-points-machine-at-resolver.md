# 03: Setup points the machine at the resolver

**What to build:** `sudo sandbox setup [--domain X]` makes the developer's operating system send lookups under the domain to the resolver, so set hostnames resolve to container addresses in any program. It records the domain in the developer's config. `--uninstall` undoes it. Spec stories 36-39, 41-45, 47.

**Blocked by:** 01, 02

**Status:** ready-for-agent

- [ ] `setup --domain X` writes `domain: X` into the invoking user's config as that user, keeping the file's other keys; without `--domain` it uses the configured domain, or `test`
- [ ] On macOS it installs the per-domain resolver configuration naming the resolver's loopback address and port
- [ ] On Linux with systemd-resolved it installs a per-domain drop-in and reloads; without systemd-resolved it refuses before installing anything and prints the line to add by hand
- [ ] Rerunning with a different `--domain` removes the previous domain's configuration and installs the new one
- [ ] Setup records which domain it installed, where `up` can read it
- [ ] Setup starts no containers and still installs the mkcert CA as the invoking user
- [ ] `setup --uninstall` removes the resolver configuration for the installed domain and stops and removes the resolver, leaves the mkcert CA, and prints the `mkcert -uninstall` hint
- [ ] Setup's summary output names what it installed
- [ ] Unit tests cover macOS, systemd-resolved, unsupported Linux, a domain change and uninstall with the fake host
