# 12: Web apps served on HTTPS at `https://<repo>.<set>.test`

**What to build:** A repo marks its web service with `x-sandbox: { web: true }` and it is served on port 443 at `https://<repo>.<set>.test`, with a certificate trusted on the host and inside every container, so cookies, CORS and OAuth callbacks behave like the committed config (stories 62, 63, 66–68, 70–72; spec: repo opt-in contract).

**Blocked by:** 06 (hosts file block), 09 (agent container and `sh`)

**Status:** done

- [x] At most one web service per repo, named after the repo; violations fail validation before any container starts
- [x] The web service gets its own IP and hostname `<repo>.<set>.test`, publishes 443 on that IP, and listens as non-root via `net.ipv4.ip_unprivileged_port_start=0`
- [x] `WORKTREE` and `APP_URL` (the primary `https://` hostname) are injected, and nothing else
- [x] A per-set `*.<set>.test` leaf certificate is issued with mkcert into the set directory and reused on later `up` while still valid
- [x] Only `rootCA.pem` is mounted into the agent and service containers, never the CA key; `up` fails early naming the fix if `rootCA.pem` is missing
- [x] `up` fails if a hostname is used twice in the set
- [x] Fixture web repo: `curl` over HTTPS from the host and from the agent container succeeds without `-k`
