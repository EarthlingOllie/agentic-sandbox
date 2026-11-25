# macOS release checklist

Run by a person on a Mac with OrbStack before each release. CI runs the walkthrough harness
on Linux (`.github/workflows/e2e.yml`); there is no macOS runner with OrbStack, so on a Mac
the same harness is run by hand. It drives the installed `sandbox` through every verb and
connects to every address every set reports (see "Development" in the README), so this list
holds only what that one command cannot check: the two commands that need your password, a
restart, the browsers, the refusals that need a different Docker runtime, and a Claude
login.

Copy this list into the release issue and tick each item. Record the macOS version, the
chip (Apple silicon or Intel), the OrbStack version (`orbctl version`) and the commit under
test.

## Before you start

- [ ] OrbStack is the running Docker runtime: `docker info --format '{{.OperatingSystem}}'`
      prints `OrbStack`, and `docker compose version` reports 2.24 or newer.
- [ ] In OrbStack's settings, under Network, "Allow access to container domains & IPs" is
      on (`orbctl config show` does not say `network_bridge: false`).
- [ ] `mkcert`, `git`, `curl` and `uv` are installed (`brew install mkcert uv`).
- [ ] The release commit is checked out and `uv sync --locked` has run in it. The commands
      below use that checkout's `.venv/bin/sandbox`.

## Setup (needs your password)

- [ ] `sudo env "PATH=$PATH" .venv/bin/sandbox setup` succeeds and prints a `resolver:` line
      naming `/etc/resolver/test` and a `mkcert:` line naming you, not root.
- [ ] `cat /etc/resolver/test` prints `nameserver 127.0.0.1` and `port 15353`, and
      `scutil --dns` lists a resolver for the domain `test` with that port.
- [ ] `mkcert -CAROOT` points into your own home, and Keychain Access lists the
      "mkcert development CA" in the login keychain.
- [ ] Only if this Mac ran a version of sandbox from before the resolver: the setup output
      says it removed `/usr/local/bin/sandbox-hosts`, the rule in `/etc/sudoers.d`, the
      launchd job `/Library/LaunchDaemons/sandbox.loopback-aliases.plist` and the `lo0`
      aliases, and any `# sandbox:` blocks in `/etc/hosts`. Afterwards none of those files is
      left over, and `ifconfig lo0` shows `127.0.0.1` as its only `inet` address.

## The harness

- [ ] Run it, keeping the setup you just installed (it needs no sudo in this mode):

      ```console
      $ SANDBOX_E2E=1 SANDBOX_E2E_KEEP_SETUP=1 uv run pytest -m e2e tests/e2e
      ```

      It ends with `1 passed` and a report whose first line reads
      `sandbox walkthrough: N checks: N passed, 0 failed, 0 not run`. Paste that line into
      the release issue. If anything failed, paste the failed checks with their output; a
      line starting `diagnostic only` says whether the resolver itself answered.
- [ ] From the report, paste the line of the `up --recreate` phase that reads
      "connect to all 7 addresses ... (took N s)". It is the measurement of how quickly
      macOS follows a recreated container; the harness allows 10 seconds.
- [ ] The harness left nothing behind: `docker ps -a --filter label=sandbox.set` lists no
      container, and `/etc/resolver/test` is still there (this mode keeps your setup).

On a Mac whose user has passwordless sudo (a CI machine, not a laptop), run it without the
second switch instead: `SANDBOX_E2E=1 uv run pytest -m e2e tests/e2e`. The harness then runs
`sudo sandbox setup` and `sudo sandbox setup --uninstall` itself and checks both.

## What the harness cannot see

Bring one set up for these. In a scratch directory, copy `tests/fixtures/web-repo` to `web`
and commit it on `main` (`git init -b main && git add -A && git commit -m initial`), then
from that directory run `sandbox rel-a up --repos web` (with the checkout's
`.venv/bin/sandbox`).

- [ ] Safari and Chrome open `https://web.rel-a.test/` and `https://admin.rel-a.test/` with
      no certificate warning. (Firefox only if it is installed and `mkcert -install` said it
      added the CA to it.)
- [ ] Restart the Mac with the set still up. After logging in and once OrbStack has
      started, without running any `sandbox` command: `docker ps --filter
      label=sandbox.resolver=true` lists `sandbox_resolver` as running, and
      `curl -sS https://web.rel-a.test/` answers with JSON.
- [ ] With "Allow access to container domains & IPs" switched off in OrbStack's settings,
      `sandbox rel-b up --repos web` fails before creating anything
      (`docker ps -a --filter label=sandbox.set=rel-b` lists nothing) and its message names
      that setting. Switch it back on.
- [ ] Only if Docker Desktop is also installed: quit OrbStack, start Docker Desktop, and
      run `sandbox rel-b up --repos web`. It fails before creating anything and its message
      names OrbStack. Quit Docker Desktop and start OrbStack again.
- [ ] `sandbox rel-a down`, then `sandbox rel-a destroy --branches`, both succeed.

## Uninstall (needs your password)

- [ ] `sudo env "PATH=$PATH" .venv/bin/sandbox setup --uninstall` succeeds, says it removed
      `/etc/resolver/test` and the resolver, and prints the `mkcert -uninstall` hint.
- [ ] `ls /etc/resolver/test` finds nothing and `docker ps -a --filter
      label=sandbox.resolver=true` lists no container.
- [ ] `sandbox rel-a up --repos web` now fails before creating anything, naming
      `sudo sandbox setup`. Run setup again if you use sandbox on this Mac.

## Claude Code (needs a Claude account)

- [ ] With a set up, `sandbox rel-a claude` starts Claude Code in the agent. The first run
      asks you to `/login`, and a second set reuses that login without asking again.
