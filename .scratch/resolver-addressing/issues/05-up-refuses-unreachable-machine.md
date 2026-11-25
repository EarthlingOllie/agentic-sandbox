# 05: `up` refuses a machine that can't reach its sets

**What to build:** Before creating anything, `up` of a set with services checks that the developer's machine can reach the set by name and says exactly what to fix if not. Spec stories 48-52.

**Blocked by:** 03, 04

**Status:** ready-for-agent

- [ ] Missing per-domain resolver configuration fails `up` with a message telling the developer to run `sudo sandbox setup`
- [ ] A configured domain that differs from the one setup installed fails `up` naming both and telling the developer to rerun setup
- [ ] On macOS, a Docker runtime other than OrbStack fails `up` explaining that containers are not reachable at their own addresses and naming OrbStack
- [ ] On macOS with OrbStack but direct container access off, `up` names the setting to turn on
- [ ] All of these fail before any clone, container or resolver is created
- [ ] A set without services passes with none of it; a set with a web app still requires the mkcert CA
- [ ] The checks for the hosts helper, its sudoers rule and the alias pool are no longer made
- [ ] Unit tests cover each failure and its message with the fake host and fake Docker
