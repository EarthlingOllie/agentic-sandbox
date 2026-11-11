# 09: Hardened agent container with worktrees mounted; `sandbox SET sh`

**What to build:** Every set includes an agent container that can work on the set's worktrees but cannot escape to the host. A developer can open a shell in it, optionally in a specific repo's worktree, or open a shell in a service container (stories 74–78, 81, 82; spec: Agent container).

**Blocked by:** 03 (Compose single repo)

**Status:** done

- [x] A default agent image ships with `sh`, `git` and `claude` on PATH and works under any UID with a writable HOME
- [x] Agent is non-root, drops capabilities, and never has the Docker socket mounted
- [x] The set directory is mounted at `/workspace/<set>/`; each source repo's `.git` is mounted read-write at its exact host path; the main working tree is never mounted; `git` works inside each worktree
- [x] On Linux the agent runs as the host UID:GID and its entrypoint adds a passwd entry, so files it writes stay owned by the developer
- [x] Host `~/.claude/` is bind-mounted read-write with `CLAUDE_CONFIG_DIR=/home/agent/.claude`; host `~/.claude.json` is not mounted
- [x] `sandbox SET sh` opens a shell in `/workspace/<set>/`; `--repo NAME` starts in that worktree
- [x] `sandbox SET sh SERVICE` opens a shell in that service; an unknown name fails and suggests `--repo`
