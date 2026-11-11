# 18: One-time `sudo sandbox setup` and prerequisite checks in `up`

**What to build:** A developer runs `sudo sandbox setup` once to install everything that needs root, after which the everyday tool is unprivileged; `--uninstall` removes it cleanly. `up` fails early, naming the missing piece, when setup hasn't been run (stories 1–11, 60; spec: CLI surface).

**Blocked by:** 06 (hosts file block), 12 (HTTPS web apps)

**Status:** done

- [x] Installs the root-owned `sandbox-hosts` helper with a NOPASSWD sudoers rule scoped to that binary only
- [ ] On macOS, installs a launchd job that creates `lo0` aliases for a pool of `--sets N` /24 blocks (default 16) at boot; on Linux no loopback setup is performed — implemented and unit-tested against a fake host (generated plist/alias script, fake `ifconfig`); needs manual verification on macOS (a Mac with sudo)
- [x] Runs `mkcert -install` as `SUDO_USER`, never as root
- [x] Refuses if the configured IP range is already in use
- [x] `--uninstall` removes helper, sudoers rule and launchd job, leaves the mkcert CA, and prints the `mkcert -uninstall` command
- [x] `up` fails before creating anything if the helper, alias pool or `rootCA.pem` is missing, naming which
- [ ] On macOS, `up` fails clearly when all alias blocks are leased, suggesting rerunning setup with a larger `--sets` — implemented and unit-tested against a fake host (fake `ifconfig lo0` aliases); needs manual verification on macOS (a Mac with sudo)
