# 06: `/etc/hosts` block per set via the `sandbox-hosts` helper

**What to build:** After a set is up, its hostnames resolve from the host: sandbox writes a `# sandbox:<set>` block to the hosts file, and `down` removes it. In production the write goes through a small privileged `sandbox-hosts` helper that refuses anything outside the set's own addresses or the `.test` TLD (stories 58, 59, 61, 97).

**Blocked by:** 05 (loopback IP leases)

**Status:** done

- [x] The hosts block is written after the set is up and contains exactly that set's hostnames and IPs; other entries in the file are never touched
- [x] `down` removes the set's block; rollback removes it too
- [x] Writes are `flock`-guarded so concurrent sets never corrupt the file
- [x] The `sandbox-hosts` helper rewrites only the named set's block and refuses IPs outside that set's /24 and names not ending in `.test`
- [x] Production path invokes the helper non-interactively (`sudo -n`); tests point the hosts path at a temp file and write directly
