# macOS release checklist

Run by a person on a Mac with sudo before each release. CI covers Linux end to end
(`.github/workflows/e2e.yml`, `tests/e2e/`), but the macOS-only paths (the launchd job that
aliases the loopback pool on `lo0`, Docker Desktop's file ownership, the login keychain and
the browsers trusting the mkcert CA) can only be checked on a real Mac.

Copy this list into the release issue and tick each item. Record the macOS version, chip
(Apple silicon or Intel), Docker Desktop version and the commit under test.

## Before you start

- [ ] Docker Desktop is running; `docker compose version` reports 2.24 or newer.
- [ ] `mkcert` is installed (`brew install mkcert`), and so are `git` and `curl`.
- [ ] `sandbox` is installed from the release commit (`uv tool install .` from a checkout).
- [ ] No earlier install is left over: `ls /usr/local/bin/sandbox-hosts
      /Library/LaunchDaemons/sandbox.loopback-aliases.plist` lists nothing, and
      `ifconfig lo0` shows only `127.0.0.1` in `127.42.0.0/16`.
- [ ] A scratch directory holds three repos copied from `tests/fixtures/`, each committed on
      `main`: `api` (from `shared-mysql-a`), `jobs` (from `shared-mysql-b`, which has the
      init hook) and `web` (from `web-repo`, with the extra host `admin`). This is the
      fixture set the Linux suite uses.

## Setup

- [ ] `sudo sandbox setup --sets 2` succeeds and prints the helper, the loopback line
      (`lo0 aliases for 2 /24 blocks`) and the mkcert line.
- [ ] `ifconfig lo0 | grep -c 'inet 127.42.'` prints `508` (127.42.0.1 to 127.42.1.254).
- [ ] `/Library/LaunchDaemons/sandbox.loopback-aliases.plist` exists, owned by root.
- [ ] `sudo -n -l /usr/local/bin/sandbox-hosts` succeeds without a password prompt.
- [ ] The mkcert CA is in the login keychain (Keychain Access, "mkcert development CA")
      and `mkcert -CAROOT` points into your own home, not root's.
- [ ] Reboot. After logging in, `ifconfig lo0 | grep -c 'inet 127.42.'` still prints `508`.

## Two sets side by side

From the scratch directory:

- [ ] `sandbox rel-a up --repos api jobs web` and `sandbox rel-b up --repos api jobs web`,
      started in two terminals at the same time, both succeed. Each prints its bindings,
      and the two sets have different IPs (each binds `mysql` to port 3306 on its own IP).
- [ ] `grep -A8 'sandbox:rel-a' /etc/hosts` shows the set's block, with
      `web.rel-a.test` and `admin.rel-a.test` on the same IP.
- [ ] `curl -sS https://web.rel-a.test/` and `curl -sS https://admin.rel-a.test/` answer
      with JSON (no `-k`, no `--resolve`): `APP_URL` is `https://web.rel-a.test`.
- [ ] Safari and Chrome open `https://web.rel-a.test/` with no certificate warning.
- [ ] From the agent: `sandbox rel-a sh` then `curl -sS https://web.rel-a.test/` answers
      the same JSON.
- [ ] The init hook ran: `sandbox rel-a sh mysql`, then
      `mysql -uroot -psandbox -e 'SHOW DATABASES'` lists `web_app`. Only one `mysql`
      container runs for the set (`docker ps --filter label=sandbox.set=rel-a`).
- [ ] Files the agent creates in a worktree (`sandbox rel-a sh --repo api`, then
      `touch from-agent`) are owned by you on the Mac.
- [ ] `sandbox status` lists both sets as running; `sandbox rel-a urls` lists both web URLs.
- [ ] A third set while two are running, `sandbox rel-c up --repos api jobs web`, fails
      before any container starts and suggests rerunning setup with a larger `--sets`.
      Run `sandbox rel-c destroy` afterwards.

## Down, destroy, uninstall

- [ ] `sandbox rel-a down` and `sandbox rel-b down` succeed. Their blocks are gone from
      `/etc/hosts`, `curl https://web.rel-a.test/` no longer resolves, and the worktrees are
      still in the sets root.
- [ ] `sandbox rel-a destroy` refuses while `api` holds the untracked `from-agent`, then
      `sandbox rel-a destroy --force --branches` removes the set directory. `git -C api
      worktree list` no longer lists it and `agent/rel-a` is gone.
- [ ] `sandbox rel-b destroy --branches` succeeds.
- [ ] `sudo sandbox setup --uninstall` removes the helper, the sudoers rule and the plist,
      and prints the `mkcert -uninstall` hint. `ifconfig lo0` no longer lists anything in
      `127.42.0.0/16`.
- [ ] `sandbox rel-a up --repos api jobs web` now fails before creating anything, naming
      `sudo sandbox setup`.

## Claude Code (needs a Claude account)

- [ ] `sandbox rel-a claude` starts Claude Code in the agent. The first run asks you to
      `/login`, and a second set reuses that login without asking again.
