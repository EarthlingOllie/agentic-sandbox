# 07: The loopback machinery is gone

**What to build:** Nothing of the loopback model remains in the product or on a machine that upgrades. Spec stories 46, 53, 59, 78.

**Blocked by:** 04, 05

**Status:** ready-for-agent

- [ ] The IP lease pool, the hosts block writer and its root helper, the loopback alias setup and its launchd job, and their tests are deleted
- [ ] `setup --sets` no longer exists
- [ ] A config that still carries `ip_pool` or `hosts_file` fails naming the key and saying it is no longer used
- [ ] `sudo sandbox setup` removes, when present, the old hosts helper and its support files, its sudoers rule, the macOS alias job and its aliases, and leftover `# sandbox:` hosts blocks; `--uninstall` does the same
- [ ] `sandbox status` finds orphans from set directories and labelled containers only; a leftover lease file is ignored
- [ ] A set created under the loopback model comes up and is reachable after `down` and `up`
- [ ] The test suite's own loopback pool helpers are gone; lint, type check and all unit and integration tests pass
