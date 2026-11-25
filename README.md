# agentic-environment-provisioning

`sandbox` provisions isolated **sets** for coding agents: the agent's own clone of each repo
on branch `agent/<set>` (synced back into your repo as `agent/<set>`), deduplicated backing
services, and a hardened agent container.
See [`.scratch/sandbox-mvp/spec.md`](.scratch/sandbox-mvp/spec.md) for the full MVP spec.

Status: early MVP. `sandbox SET up --repos NAME...` creates the set directory, writes its
plan, clones each repo for the agent, then starts the services a repo declares in
`.sandbox/compose.yaml` as Compose project `sandbox-<set>` and waits until they are healthy
(a repo without `compose.yaml` takes part with its clone only). Inside the set, services
reach each other by their short names (`mysql`). `sandbox SET down` stops the containers and
removes the set's network and volumes; the clones and the plan are kept. Repos that declare
the same service share one instance of it (see [Shared services](#shared-services)). From
your machine every service answers at its set hostname, `<service>.<set>.test`, on its
conventional port, in every running set at once; nothing is published on the host (see
[Addressing](#addressing)). How the agent's commits reach your repo is in
[The agent's clones](#the-agents-clones).

```console
$ cd ~/code            # holds ./api and ./web, each with a .sandbox/ directory
$ sandbox jira-123 up --repos api web
set jira-123: /home/me/sandbox/sets/jira-123 (provisioned)
  plan: /home/me/sandbox/sets/jira-123/sandbox.yaml (created)
  api: /home/me/sandbox/sets/jira-123/api [agent/jira-123, created from main]
  web: /home/me/sandbox/sets/jira-123/web [agent/jira-123, created from release/2.4]
  services: mysql, web healthy [sandbox-jira-123]
  hostnames:
    mysql.jira-123.test  3306/tcp
    web.jira-123.test  443/tcp
  agent: running [sandbox-jira-123]; open a shell: sandbox jira-123 sh
$ mysql -h mysql.jira-123.test -uroot -p   # from your machine, on 3306
$ sandbox jira-123 sh --repo api   # a shell in the agent, in /workspace/jira-123/api
$ sandbox jira-123 sh mysql        # a shell in the mysql service container
$ sandbox jira-123 claude          # Claude Code in the agent, in /workspace/jira-123
$ sandbox jira-123 down
set jira-123: sandbox-jira-123 stopped; clones kept in /home/me/sandbox/sets/jira-123
$ git -C api log --oneline -1 agent/jira-123   # the agent's commits, synced by down
```

## Addressing

Every service of a set, every extra host of a web app and the set's agent has a **set
hostname**, `<name>.<set>.<domain>`. The **domain** is `test` unless you chose another at
setup (see [Configuration](#configuration)); this README writes hostnames with `test`.

```text
mysql.jira-123.test:3306      mysql.jira-456.test:3306      # two sets, one conventional port
https://web.jira-123.test     https://web.jira-456.test
agent.jira-123.test           agent.jira-456.test
```

- **A set hostname resolves to the container's own address.** A set publishes nothing on
  the host: sandbox allocates no IPs and no ports, so every service keeps its conventional
  port (3306 for MySQL) in every set, and the number of sets is limited only by Docker.
- **The resolver answers the lookup.** One shared container, the resolver, belongs to no
  set and answers lookups of set hostnames from your machine by asking Docker's own DNS. It
  carries no traffic and there is no reverse proxy: your client asks for the name, then
  connects straight to the container. `up` starts the resolver if it is not running and
  attaches it to the set; `down` detaches it, so only running sets resolve. It comes back
  with Docker after a restart of the machine (see
  [The resolver and isolation between sets](#the-resolver-and-isolation-between-sets)).
- **The hostname is the stable address.** A container's address changes whenever it is
  recreated (`down` and `up`, `up --recreate`), and the name follows it within seconds (the
  resolver caches nothing and answers with a one-second lifetime); connect to the name,
  never to the address. Once a set is down its hostnames stop
  resolving, and an unknown name under the domain fails at once instead of hanging.
- **The same name works inside the set.** The agent and the services reach
  `mysql.jira-123.test:3306` exactly as your machine does. Inside the set the short name
  (`mysql`) works too, so committed app config needs no change.
- **`ports:` is a declaration, not a gate.** The container ports a service declares are what
  `sandbox SET urls` lists. Any host-side binding a repo wrote next to them
  (`"0.0.0.0:3306:3306"`) is ignored: it neither collides between sets nor opens a port on
  your network.

### What your machine can reach

Because containers are reached at their own addresses, your machine can connect to **every
port a container listens on**, in every running set, whether or not the service declares it
under `ports:`. That includes the agent: something the agent serves on port 3000 is at
`agent.<set>.test:3000`. Nothing is published on the machine's own interfaces: no port of
your machine leads into a set. Containers of one set still cannot connect to containers of
another.

### Supported hosts

This needs a machine from which containers are reachable at their own addresses:

- **Linux.** `sudo sandbox setup` points systemd-resolved at the resolver. On Linux without
  systemd-resolved, setup installs nothing and prints the one line to add to your own DNS
  server (see [Setup](#setup)).
- **macOS with [OrbStack](https://orbstack.dev)**, with its "Allow access to container
  domains & IPs" setting on (the default).

**Docker Desktop for Mac is not supported**: its containers are not reachable at their own
addresses, so a set hostname would lead nowhere. There, `up` refuses a set with services
before it creates anything and names OrbStack; with OrbStack's direct container access
switched off, it names that setting. Why this model was chosen, and what was given up for
it, is in
[ADR 0001](docs/adr/0001-set-hostnames-resolve-to-container-addresses.md).

### Upgrading from an earlier version

Earlier versions gave every set hostname a loopback address of its own and wrote the names
to `/etc/hosts` through a root-owned helper; all of that is gone. To move over:

1. Rerun `sudo sandbox setup`. Besides pointing the machine at the resolver, it removes
   what the earlier version installed and nothing uses any more: the `sandbox-hosts` helper
   and its rule in `/etc/sudoers.d`, the macOS launchd job with its `lo0` aliases, and any
   `# sandbox:` blocks left in `/etc/hosts`.
2. Remove `ip_pool` and `hosts_file` from `~/.config/sandbox/config.yaml` if you had set
   them. They are no longer used, and sandbox refuses a config that still carries one,
   naming the key. The `--sets` option of setup is gone too, since there is no pool to size.
3. Bring each existing set `down` and `up` again. There is no other migration step.

## The agent's clones

The agent never works in your repo. `up` gives it a clone of each repo at
`<set dir>/<repo>` (`git clone --shared`: it borrows your repo's objects read-only through
`objects/info/alternates`, so it costs next to nothing), checked out on a new branch
`agent/<set>` cut from the plan's `base`, or on your repo's existing `agent/<set>` if one
survived an earlier set. Its `origin` is your repo, so the agent can `git fetch origin` to
see your latest work. In its clone the agent commits, branches, rebases and configures git
as it likes; none of it is visible in your repo until sandbox **syncs** it.

`up`, `sandbox SET status`, `down` and `destroy` sync: they fetch the clone's
`agent/<set>` into your repo's `agent/<set>` (and record the agent's tip as
`refs/sandbox/agent/<set>`, which keeps its objects safe from `git gc`). So
**`agent/<set>` in your repo is not live**: it moves only when one of those commands runs.
Only `agent/<set>` comes back; the agent's other branches stay in its clone. A sync never
loses work:

- it fast-forwards your `agent/<set>`, or follows history the agent rewrote (an amend, a
  rebase) if you haven't touched the branch since the last sync;
- if you committed on `agent/<set>` yourself, or have it checked out, it leaves the branch
  alone and says so (`up` and `down` warn, `status` notes it); the agent's commits are at
  `refs/sandbox/agent/<set>` for you to merge. `destroy` refuses such a set unless
  `--force`.

Sets created by earlier versions of sandbox (whose repos were `git worktree`s of your repo)
show their repos as missing: `destroy` them (and `git worktree prune` in each repo) and
`up` them again.


`up` renders a per-set `override.yaml` into the set directory and runs
`docker compose -p sandbox-<set> -f <api>/.sandbox/compose.yaml -f <web>/... -f override.yaml up -d --wait`,
with every repo's original file (in repo-name order) before the override. The override
labels every service, network and declared volume with `sandbox.set=<set>`.

### Shared services

Services are matched **by name only** (never by image): if `api` and `web` both declare
`mysql`, the set runs one `mysql`. Before anything is created, each repo's compose file is
normalised on its own with `docker compose config` (so `extends`, short/long syntax and
list/map `environment` don't cause false conflicts) and same-named services are compared:

- `depends_on`, `labels`, `ports` and `x-sandbox` are ignored (sandbox publishes no ports), as are
  init-hook mounts under `/sandbox/init/` (see below);
- a `healthcheck` set by only one repo is kept; two different healthchecks conflict;
- any other difference fails `up`, before any container starts, with a diff naming both repos:

```console
$ sandbox jira-123 up --repos api legacy
error: service 'mysql' is defined differently by api and legacy:
  --- api: mysql
  +++ legacy: mysql
  @@ -6,7 +6,7 @@
  ...
  -image: mysql:8.4
  +image: mysql:8.0
```

**Multi-version services are not supported in the MVP.** Two repos that need different
versions of a service (e.g. `mysql84` beside `mysql`, or `mysql:8.0` vs `mysql:8.4` under
one name) cannot share a set; run them in separate sets.

Compose resolves the relative paths of *every* `-f` file from the first file's directory,
so for a service that only a later repo declares, the override restates that repo's
normalised definition (absolute paths, `env_file` inlined into `environment`) with
Compose's `!override`/`!reset` merge tags (Docker Compose 2.24 or newer). One limitation
remains: `extends: {file: ...}` is resolved while Compose loads the files, so only the
first repo (by name) of a multi-repo set may use it; later repos should extend services
defined in the same file.

### Init hooks

A repo can ship `.sandbox/init/<service>.sh` to create its own database, users or seed data
in a (possibly shared) service. Sandbox mounts the repo's `.sandbox/init/` read-only at
`/sandbox/init/<repo>/` in that service and, once `compose up --wait` reports the set
healthy, runs `sh /sandbox/init/<repo>/<service>.sh` with `docker compose exec`, in
repo-name order. Hooks run on every `up` that starts the set (first `up`, resume,
`--recreate`), so write them to be idempotent:

```sh
# api/.sandbox/init/mysql.sh
mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS api"
```

A hook for a service the set does not run fails `up` before any container starts. A hook
that exits non-zero fails the set and rolls it back (plan and clones are kept); the
error shows the hook's output. Hooks are read from the repo's source checkout, like its
compose file.

### HTTPS web apps

A repo marks its web service with the `x-sandbox` extension. The service must be named after
the repo, and a repo has at most one:

```yaml
# web/.sandbox/compose.yaml
services:
  web:
    build: ..
    x-sandbox:
      web: true # served at https://web.<set>.test
      hosts: [admin, portal] # optional: https://admin.<set>.test, https://portal.<set>.test
      vite: 5173 # optional: Vite dev server port
```

Sandbox then:

- serves it at `https://<repo>.<set>.test`: the service's set hostname, at the container's
  own address, with nothing published and no proxy in between, so what you see is exactly
  what the app serves. The app listens on 443 itself, as non-root, because the service gets
  `net.ipv4.ip_unprivileged_port_start=0`;
- issues a per-set `*.<set>.test` certificate with mkcert into `<set dir>/tls/` and mounts
  it read-only at `/sandbox/tls/cert.pem` and `/sandbox/tls/key.pem` (the key is readable
  by any user, so the app can run as whatever user its image uses). Later `up`s reuse it
  while it verifies against the CA and has more than 7 days left;
- injects `WORKTREE` (the repo's checkout in the set, the agent's clone at `<set dir>/<repo>`) and `APP_URL`
  (`https://<repo>.<set>.test`) into the service's environment, and nothing else (plus
  `VITE_HMR_HOST` with `vite`, below);
- makes each extra host in `hosts` (`admin` becomes `admin.<set>.test`) a further set
  hostname of the same container: it is a network alias of the service, so the resolver
  answers it with the web app's address, and the set's wildcard certificate covers it. That
  is why an extra host must be a single DNS label (`admin`, not `eu.admin`);
- with `vite: <port>`, sets `VITE_HMR_HOST` to the app's hostname; the dev server is reached
  on that port at the same hostname (`https://web.<set>.test:5173`), again with nothing
  published. Point Vite's HMR at it, e.g. in `vite.config.js`:
  `server: { host: true, port: 5173, hmr: { host: process.env.VITE_HMR_HOST } }`;
- mounts the mkcert CA's `rootCA.pem`, never `rootCA-key.pem`, read-only at
  `/sandbox/ca/rootCA.pem` in every container of the set. The bundled agent image trusts it
  system-wide (curl, git) and through `NODE_EXTRA_CA_CERTS`; a service image decides itself
  whether to trust it.

The host trusts the certificate once `sudo sandbox setup` has installed the mkcert CA
(`mkcert -install`). `up` fails before any container starts if a set with a web app finds
no `rootCA.pem` in mkcert's `CAROOT`, if a repo marks more than one service or a service not
named after the repo, if an extra host is more than one label deep, or if a hostname is used
twice in the set (e.g. another repo runs a service with the web app's name, or an extra
host matches a service, the agent or another web app). Sets without a web app need neither
mkcert nor the CA. The certificate is issued for the configured domain: a set whose
certificate was issued for another domain gets a new one on its next `up`.

### The agent container

Every set also runs a hardened `agent` service (a repo may not declare a service of that
name). `sandbox SET sh` opens a shell in it at `/workspace/<set>/`; `--repo NAME` starts in
the agent's clone of that repo. `sandbox SET sh SERVICE` opens a shell in one of the set's services
instead; an unknown name fails and points at `--repo`.

- **Image**: the bundled default (`src/sandbox/images/agent/`: Ubuntu 24.04 with `sh`,
  `git` and `claude` on PATH). Compose builds it on the first `up` as
  `sandbox-agent:<content hash>` and never pulls it from a registry. It works under any
  UID with a writable `HOME` (`/home/agent`): its entrypoint adds a passwd/group entry for
  the UID:GID it runs as.
- **User**: your host UID:GID, so files the agent writes in its clones and in
  `~/.claude/` stay yours. If sandbox itself runs as root, the agent runs as `1000:1000`;
  it never runs as root.
- **Hardening**: `cap_drop: [ALL]`, `no-new-privileges`, and the Docker socket is never
  mounted.
- **Hostname**: the agent answers at `agent.<set>.test`, from your machine and from the
  set's services. It declares no ports; whatever it listens on is reachable there (see
  [What your machine can reach](#what-your-machine-can-reach)).
- **Mounts** (read-write binds unless noted): the set directory at `/workspace/<set>/`,
  which holds the agent's clones (its `sandbox.yaml` read-only); each source repo's `.git`,
  **read-only as a whole**, at its exact host path, because the clones borrow its objects
  and fetch from it there. The agent can write nothing in your repo's `.git`: not its
  config or hooks, and not a symlink under `refs/`, `logs/` or `objects/` that git on the
  host (sandbox's, or your own) would later write through. Everything in a clone, `.git`
  included, is the agent's, so sandbox's host git treats a clone as an untrusted repository:
  syncing writes only in your repo and never runs git on the clone's own `.git`: sandbox
  copies the clone's `agent/<set>` tip and its own objects (loose objects and packs, read
  without following any symlink and skipping FIFOs and devices) into a git dir it writes
  itself, whose only `alternates` is your repo's, and fetches from that through `git
  upload-pack --strict`. So a clone whose `alternates`, `objects`, `refs`, `packed-refs` or
  `commondir` point at another repository on the host brings none of its objects into your
  repo (which the agent could then read), a FIFO in the clone blocks nothing, and a clone
  whose `.git` is a symlink or file isn't read at all. `status` reads a clone's `HEAD`,
  branch and index the same way into such a git dir, so the clone's config and hooks are
  never read. Host git commands also time out rather than hang. Every git
  command sandbox runs on the host also uses isolated config (no system or global config,
  nothing from `GIT_*` variables, and hooks, fsmonitor, editors, pagers and other
  program-running settings pinned off). (So filters such as Git LFS's, configured in your
  global config, don't run when sandbox checks out a clone.) Treat running git yourself in
  a clone on the host like running it in a repo you cloned from a stranger; in your own
  repo it is safe. Also: the host's `~/.claude/` at
  `/home/agent/.claude` with `CLAUDE_CONFIG_DIR=/home/agent/.claude`, so all sets share one
  login. Your main working trees and `~/.claude.json` are never mounted. In a set with a
  web app, the mkcert CA's `rootCA.pem` is also mounted read-only (see above).

### Claude Code in the agent

`sandbox SET claude` runs Claude Code interactively in the set's agent, starting in
`/workspace/<set>/`. It fails with a clear message (and the `up` command to run) if the set
isn't running. Every set shares one Claude login through the `~/.claude/` mount: the first
time no `~/.claude/.credentials.json` exists, sandbox says so and Claude Code leads you
through `/login`, which writes `.credentials.json` into that directory; every other set
then starts already logged in. sandbox keeps that file at mode `0600`.

```console
$ sandbox jira-123 claude    # first time: log in with /login
$ sandbox jira-456 claude    # already logged in
```

Whatever follows the verb is passed to Claude Code as it is, so the non-interactive forms
work too. `--help` is the verb's own; put `--` before it to get Claude Code's.

```console
$ sandbox jira-123 claude --version
$ sandbox jira-123 claude -p "run the tests and fix what fails"
$ sandbox jira-123 claude -- --help
```

Accepted trade-offs:

- **Plaintext token on macOS.** Claude Code on a Mac host keeps its login in the Keychain,
  which a Linux container cannot reach, so the agents log in separately and store their
  token in plaintext in `~/.claude/.credentials.json` (owner-only, `0600`), where any process
  running as you can read it. (On Linux hosts Claude Code already uses
  this file.)
- **Occasional forced re-login.** Concurrent sets share one token file. When two agents
  refresh the token at the same moment, one refresh can invalidate the other's token, and
  that session has to `/login` again. Logging in again from any set fixes it for all.

Linux and macOS only.

### Agent overrides

Bring your own agent image, or add sidecars such as an egress proxy, with an `agent`
Compose fragment (keys `services`, `volumes`, `networks`) in the global config, a template
or a set plan. The set plan wins, then the template, then the config: whichever applies
when the set is created is **pinned into its plan**, like `source`, so later config edits
don't change an existing set (edit its `sandbox.yaml` instead).

`services.agent` is merged over the default agent, then sandbox **re-applies its
guarantees** on top: the mounts above (plus the CA certificate, once sets serve HTTPS),
the host UID:GID as `user`, `CLAUDE_CONFIG_DIR` and `HOME`, `cap_drop: [ALL]` and
`no-new-privileges`. An override can't run the agent as root, add capabilities
(`privileged`, `cap_add`, `devices`, `pid`/`ipc`/`userns_mode`, `network_mode`,
`volumes_from` are removed), or mount the Docker socket (a `docker.sock` or a directory
holding one, such as `/var/run`). `up` warns about each setting it overrides. Extra
volumes and environment variables are kept.

A custom `image` replaces the bundled one and is pulled like any other image (`build` is
not supported: build it yourself and set `image`). It must work under any UID with a
writable `HOME` and have `sh`, `git` and `claude` on its PATH: `up` runs it once with
`docker run` to check, and fails before any container starts if one is missing.

Every other service in the fragment runs with the set, labelled with it, and joins the
set network (its `network_mode` is dropped; `default` is added to its `networks`), so the
agent and the repos' services reach it by name. A fragment service may not reuse the name
of a repo's service.

**Egress is unrestricted in the MVP**: the agent can reach any host the Docker host can.
To restrict it, bring your own proxy, for example:

```yaml
# ~/.config/sandbox/config.yaml (or under `agent:` in a template or set plan)
agent:
  services:
    agent:
      environment:
        HTTPS_PROXY: http://proxy:3128
        HTTP_PROXY: http://proxy:3128
        NO_PROXY: localhost,127.0.0.1,mysql
    proxy:
      image: ubuntu/squid:6.6-24.04_edge
      volumes:
        - /home/me/sandbox/squid.conf:/etc/squid/squid.conf:ro   # your allowlist
```

Proxy variables are honoured by `git`, `claude` and most tools, but nothing stops a
process from ignoring them: the agent stays on the set network, which has a route out.
Enforced egress restriction is out of scope for the MVP.

## Status

`sandbox status` lists every set with its state; `sandbox SET status` shows one set's
services (container state, health, set hostname and, while the service runs, its
container's current address) and the agent's clones (branch, plus `*` for uncommitted
changes and `?` for untracked files). It first syncs the agent's `agent/<set>` into each
repo, and notes any repo it couldn't sync (`sync` and `sync_detail` in `--json`). `--json`
prints the same Pydantic models the tables are rendered from; the address (`ip`) is `null`
for a service that is not running.

```console
$ sandbox status
SET       STATE     DETAIL
jira-123  running
jira-456  partial   not running: redis
spike-01  orphaned  2 containers with no set directory
$ sandbox jira-123 status
set jira-123: running [sandbox-jira-123]

SERVICE  STATE    HEALTH   HOSTNAME             ADDRESS
agent    running  -        agent.jira-123.test  172.18.0.2
mysql    running  healthy  mysql.jira-123.test  172.18.0.4
web      running  healthy  web.jira-123.test    172.18.0.3

REPO  BRANCH          CHANGES
api   agent/jira-123  * uncommitted changes
web   agent/jira-123  clean
```

The address is read from Docker when the command runs and is shown for information: it
changes whenever the container is recreated, and a stopped set shows its hostnames with `-`
in its place. Connect to the hostname.

States are reconciled from the set directories and the containers Docker labels
`sandbox.set=<set>` (queried with the `docker` CLI), and nothing else: **running** (every
service and the agent running), **stopped** (a plan, nothing running), **partial** (some
running) and **orphaned** (containers whose set has no plan, or a set directory without
one).

## URLs

`sandbox SET urls` lists every address of a set: web apps as `https://` URLs (extra hosts
and a Vite dev server included), other services as `<service>.<set>.test:<port>` on each
container port they declare (without a port if they declare none), and the agent's
hostname. Next to each is its container's current address. A stopped set lists the same
addresses, marked as not running and without container addresses. `--json` prints the same
Pydantic model the table is rendered from, with `ip` set to `null` when nothing runs there.

```console
$ sandbox jira-123 urls
set jira-123: running

WEB                             IP
https://web.jira-123.test       172.18.0.3
https://admin.jira-123.test     172.18.0.3
https://web.jira-123.test:5173  172.18.0.3

SERVICE                   IP
mysql.jira-123.test:3306  172.18.0.4
redis.jira-123.test:6379  172.18.0.5
worker.jira-123.test      172.18.0.6

AGENT                IP
agent.jira-123.test  172.18.0.2
```

Every address in the list works from your machine and from inside the set. The list is
what the services declare, not a limit on what answers (see
[What your machine can reach](#what-your-machine-can-reach)).

## Prune

`sandbox SET prune` detaches the resolver from a stopped set and removes the set's
containers, network and volumes. Nothing else describes where a set is, so there is nothing
else to release. It refuses while any of the set's services run (`sandbox SET down` first),
or while an `up` of the set is in progress. The set directory, plan and the agent's clones
are never touched, and the resolver keeps running for the other sets.

Bare `sandbox prune` does the same for every **orphaned** set `status` reports (leftovers
of crashes). A set whose `up` is still in progress is skipped. Sets are pruned concurrently
(at most 4 at a time) and independently: a set that fails is reported, keeps its containers
so a rerun retries it, and the rest carry on; the command then exits 1.

## Destroy

`sandbox SET destroy` removes a set for good: it syncs each clone's `agent/<set>` into your
repo one last time (see [The agent's clones](#the-agents-clones)), prunes the set (as
above), deletes the agent's clones and the set directory with its plan and certificate,
and drops `refs/sandbox/agent/<set>`. Your repos hold nothing else of the set. Every
check runs before anything is removed, so a refused `destroy` changes nothing (beyond the
sync):

- a set with anything running is refused, even with `--force` (`sandbox SET down` first);
- a repo whose `agent/<set>` couldn't be synced (you committed on the branch too, have it
  checked out, or the fetch failed) is refused, naming it, unless `--force` (which
  discards the agent's commits that weren't synced);
- a clone with uncommitted changes or untracked files is refused, naming the repo and
  the paths, unless `--force` (which discards them);
- the `agent/SET` branches are kept by default. `--branches` deletes them too, but refuses
  a branch holding commits that are on no remote-tracking branch, other branch or tag
  (named in the error) unless `--force` is also given.

Containers can leave root-owned files in a clone (a service writing to a bind mount as
root). On Linux, `destroy` deletes those from a throwaway root container
(`docker run --rm ubuntu:24.04 ...`, the default agent image's base), so sudo is never
needed. On macOS, OrbStack presents the files containers write in a bind mount as the
developer's own, so this is not expected to arise there.

## The resolver and isolation between sets

One shared container, the resolver (`sandbox_resolver`), answers lookups of set hostnames
(see [Addressing](#addressing)). It is dnsmasq, built by Compose from a definition that
ships with sandbox (`src/sandbox/images/resolver/`, tagged by content hash) and never
pulled from a registry. `up` starts it when it is not running, concurrent `up`s start
exactly one, and it restarts with Docker, which restores the set networks it was on, so
running sets resolve again after a reboot without another command. `down`, `prune` and
`destroy` of one set leave it running for the others; only `sudo sandbox setup --uninstall`
removes it.

It is attached to the network of every running set, so every set's containers can send it
packets. A container that deliberately queries the resolver directly, instead of using the
DNS Docker gives it, can therefore learn the hostnames and addresses of other sets'
services. It still cannot connect to them: each set has its own network and Docker does
not route between them, and the resolver only answers names, it carries no traffic. The
resolver runs as an unprivileged user with every capability dropped, no-new-privileges, a
read-only root filesystem, no Docker socket and no mounts; it is published on the
machine's loopback only (`127.0.0.1:15353`), refuses names outside the domain and never
forwards a name to an upstream DNS server.

## Setup

Everything that needs root is done once, after which `sandbox` runs unprivileged: `up` and
`down` never use sudo.

```console
$ sudo sandbox setup              # or: sudo env "PATH=$PATH" sandbox setup, if sudo's PATH lacks it or mkcert
sandbox setup installed:
  resolver: names under .test are looked up at 127.0.0.1 port 15353 (/etc/systemd/resolved.conf.d/sandbox.conf, systemd-resolved.service reloaded)
  mkcert: CA installed for me (`mkcert -install` as me)
$ sudo sandbox setup --domain box # choose another domain; recorded in your config
$ sudo sandbox setup --uninstall
```

- **Resolver configuration.** Setup tells the operating system to send lookups of names
  under the domain, and only those, to the resolver at `127.0.0.1` port `15353`, so set
  hostnames resolve in every program:
  - on **macOS**, the per-domain resolver file `/etc/resolver/<domain>`;
  - on **Linux with systemd-resolved**, the drop-in
    `/etc/systemd/resolved.conf.d/sandbox.conf` (`DNS=127.0.0.1:15353`, `Domains=~<domain>`;
    systemd 246 or newer), followed by a reload of systemd-resolved;
  - on **Linux without systemd-resolved**, setup refuses before installing anything and
    prints the line to add to your own DNS server, for dnsmasq
    `server=/test/127.0.0.1#15353`. `up` cannot check such a machine: it warns and carries on.
- **The domain.** `--domain X` chooses the DNS suffix of set hostnames and writes
  `domain: X` into your config (`~/.config/sandbox/config.yaml`), as you, leaving the
  file's other keys alone. Without it setup uses the configured domain, `test` by default.
  Rerunning setup with another `--domain` switches over: the previous domain's
  configuration is removed. `local` is refused, because mDNS answers those names.
- **mkcert CA.** `mkcert -install` runs as you (`SUDO_USER`), never as root, so the CA lands
  in your own CAROOT and your browser trusts set certificates. mkcert must be on sudo's
  `PATH`.
- **Nothing is started.** Setup starts no containers; the resolver itself is started by the
  first `up`.
- **Leftovers of an earlier version are removed**: the `sandbox-hosts` helper and its rule
  in `/etc/sudoers.d`, the macOS launchd job with its `lo0` aliases, and `# sandbox:` blocks
  in `/etc/hosts` (see [Upgrading from an earlier version](#upgrading-from-an-earlier-version)).
- `--uninstall` removes the resolver configuration (of whichever domain was installed) and
  the resolver container, and the same leftovers. It leaves the mkcert CA, which other tools
  may use, and prints `mkcert -uninstall` for you to run yourself if you want it gone.

`up` checks, before it creates anything, that the machine can reach the set, and names what
is wrong together with its fix: no resolver configuration (run `sudo sandbox setup`), a
configuration for a domain other than the configured one (rerun setup with `--domain`), on
macOS a Docker runtime that is not OrbStack or an OrbStack with direct container access
off, and for a set with a web app a missing mkcert `rootCA.pem`. Sets without services need
none of it.

```console
$ sandbox jira-123 up --repos api web
error: this machine cannot reach the set:
  it does not send names under `test` to sandbox's resolver (there is no per-domain resolver configuration); run `sudo sandbox setup`
```

The Linux path is exercised for real by the walkthrough harness (see
[Development](#development)). The macOS path (`/etc/resolver`, OrbStack detection) is
unit-tested against a fake host; a person verifies it before each release by running the
same harness on a Mac with OrbStack and working through the
[macOS release checklist](docs/release-checklist-macos.md).

## Configuration

`~/.config/sandbox/config.yaml` (override the path with `SANDBOX_CONFIG`). Every key is
optional; unknown keys and bad values fail with field-level errors.

```yaml
domain: test                      # set hostnames are <name>.<set>.<domain>; set by `setup --domain`
templates_root: ~/.config/sandbox/plans
sets_root: ~/sandbox/sets
state_dir: ~/.local/state/sandbox # lock files and other cross-process host state
agent: null                       # default agent override (see Agent overrides)
```

`domain` is the DNS suffix every set hostname ends in. This README writes hostnames with the
default, `test` (`mysql.jira-123.test`); with `domain: sandbox.test` the same service is
`mysql.jira-123.sandbox.test`, a web app is served at `https://<repo>.<set>.sandbox.test`,
and the set's certificate is issued for `*.<set>.sandbox.test` (a set whose certificate was
issued for another domain gets a new one on its next `up`). It is one or more labels of
lowercase letters, digits and inner hyphens; `local` and anything under it are refused,
because mDNS answers those names.

The operating system must send that domain to the resolver, which is what
`sudo sandbox setup --domain X` arranges, writing the key here as it does. Change the domain
through setup rather than by editing this file: `up` compares the two and refuses, naming
the command to rerun, when the configured domain is not the one setup installed. Pick a
domain no other tool on your machine claims; `test` is a top-level name reserved for
testing, so no public site answers under it.

## Plans

A set's plan lives at `<sets_root>/<set>/sandbox.yaml`. The set's name is its directory
name and never appears in the plan. `up` never overwrites a plan and refuses a `--repos`
list that differs from the one on disk; edit the plan to change the set.

```yaml
repos:
  api: { source: /home/me/code/api, base: main }
  web: { source: /home/me/code/web, base: release/2.4 }
```

## Templates

A template is a plan stored as `<templates_root>/<NAME>.yaml`. It shares the set plan's
schema, except that `base` is optional. There is no template inheritance: each template is
a complete plan.

```console
$ sandbox save --to stack --repos api web   # from ./api and ./web; absolute sources, no base
$ sandbox jira-123 save --to stack-pinned   # an existing set's plan, bases included
$ sandbox templates                          # list them
stack
stack-pinned
$ sandbox jira-456 up --from stack          # works from any directory
```

`up --from TEMPLATE` copies the template into the new set's plan. A repo without `base`
gets the branch checked out in its source at that moment, pinned into the set plan so the
set stays reproducible. Sources are the template's absolute paths, so the current
directory does not matter. Every repo is checked (`.sandbox/` present, git repo root)
before anything is created. `--from` and `--repos` are mutually exclusive, and `save`
never overwrites an existing template.

## Development

```console
$ uv sync                          # Python 3.12+, managed by uv
$ uv run ruff format --check . && uv run ruff check .
$ uv run pyright                   # strict mode
$ uv run pytest -m "not integration"   # unit: fakes, runs on Linux and macOS
$ uv run pytest -m integration         # integration: real git + Docker Compose (Linux in CI)
$ SANDBOX_E2E=1 uv run pytest -m e2e   # walkthrough harness: sudo setup, every verb (changes the machine)
```

CI (`.github/workflows/ci.yml`) runs all of the above except the walkthrough harness on
every push: lint and types, the unit tests on Linux and macOS (no Docker, no root), and the
integration tests on Linux, where a real resolver answers for real sets (asked directly, on
its loopback port, so no setup is needed).

The **walkthrough harness** (`tests/e2e/`) drives the installed `sandbox` CLI as a user
would, on a real machine, through every verb: `sudo sandbox setup`, `up --repos`, `save`
(from repos and from a set), `templates`, `up --from`, a repeated `up`, `up --recreate`,
`status`, `SET status` and `urls` (each also with `--json`), `sh`, `sh --repo`,
`sh SERVICE`, `claude --version`, bare `prune`, `down`, `destroy` (its refusals, `--force`,
`--branches`), `SET prune` and `sudo sandbox setup --uninstall`. Each step checks the exit
status and what the verb promises, and the run ends with a report: every command, what was
checked, pass or fail, and the output of anything that failed. A failed check does not stop
the steps that do not depend on it, and whatever the run created is always removed.

It also proves that every address a set reports is usable. For each running set it connects
from the machine to every address `sandbox SET urls --json` prints: an HTTPS request
verified against the mkcert CA for web apps and their extra hosts, a login and a query for
MySQL, a ping for Redis, a TCP connect for anything else, including a service that declares
no ports and the agent, on a port they listen on. It runs two sets at once and reads each
set's own data back from MySQL on 3306 and Redis on 6379, repeats the connections from
inside each agent, checks that a container of one set cannot connect to the other set's
services, that every address answers again within seconds of `up --recreate`, that the
names stop resolving after `down`, and that an unknown name under the domain fails fast.
Names are resolved through the operating system only; the resolver is asked directly just
to explain a failed check in the report.

It changes the machine it runs on (the resolver configuration, the mkcert CA), so it runs
only with `SANDBOX_E2E=1`, as a non-root user with passwordless sudo, on Linux with
systemd-resolved or on macOS with OrbStack. On a machine that is already set up, add
`SANDBOX_E2E_KEEP_SETUP=1`: the harness then runs neither `setup` nor `setup --uninstall`,
needs no sudo, and uses the domain your setup installed. Either way it keeps its sets and templates in a temp directory,
never yours.

```console
$ SANDBOX_E2E=1 uv run pytest -m e2e tests/e2e                           # throwaway machine
$ SANDBOX_E2E=1 SANDBOX_E2E_KEEP_SETUP=1 uv run pytest -m e2e tests/e2e  # keep your setup
```

Its workflow (`.github/workflows/e2e.yml`) runs it on a GitHub-hosted Ubuntu runner nightly,
on demand and for `v*` tags, not on every push, and puts the report on the run's summary
page. Before each release a person runs the same harness on a Mac with OrbStack; the
[macOS release checklist](docs/release-checklist-macos.md) says how, and lists the few
things no command can check.

### Conventions

- **Docker is driven through its CLIs only.** All Docker interaction goes through the
  `docker` and `docker compose` command-line tools via `subprocess` (asyncio). The Python
  Docker SDK (`docker` on PyPI) is not, and must not become, a dependency.
- **Git is driven through the `git` CLI** behind the `sandbox.git.Git` protocol, so unit
  tests can substitute `tests.fakes.FakeGit`.
- **Compose is driven through `docker compose`** behind the `sandbox.compose.cli.Compose`
  protocol; unit tests substitute `tests.fakes_compose.FakeCompose`. Compose code lives in
  the `sandbox.compose` package, one module per concern (`cli`, `services`, `override`,
  ...); a new concern adds a module and registers its fragment in `override.FRAGMENTS`.
- **One module per verb**: verbs other than `up` live in `sandbox/commands/<verb>.py` and
  are registered in `sandbox/cli.py` with one `app.command` line; shared helpers
  (`Deps`, `current_set`, error exits) are in `sandbox/context.py`.
- **Primary test seam: the CLI, in-process.** Tests call
  `CliRunner().invoke(app, args, obj=Deps(git=..., compose=...))` (see `tests/conftest.py`) with
  `SANDBOX_CONFIG` pointing at a config that puts every host-state location (sets root,
  templates root, state dir) in a temp directory. Unit runs pass fakes in `Deps`;
  integration runs (`@pytest.mark.integration`) pass the real implementations through the
  same seam. Assert on exit codes, output and the files sandbox writes, not on internals.
- **`up` is an ordered step pipeline** (`sandbox/pipeline.py`). A new check or action is a
  `Step(name, run, rollback=...)` in its own module plus one line in
  `provisioner.VALIDATION_STEPS` (checks that create nothing; all finish before any container
  starts) or `provisioner.PROVISION_STEPS` (actions; on failure or Ctrl-C the started ones roll
  back in reverse order, shielded from cancellation, keeping plan and clones). Per-repo
  fan-out uses `pipeline.fail_fast` (a `TaskGroup`). The state table is in `sandbox/state.py`.
- **Host state is shared across processes**, so writes to it are guarded by `flock`
  (`sandbox.locking.file_lock`); creating a clone and syncing `agent/<set>` are serialised per source repo.
- **User-facing failures raise `SandboxError`**; the CLI prints `error: <message>` and
  exits 1 (usage errors exit 2). No tracebacks for expected failures.
