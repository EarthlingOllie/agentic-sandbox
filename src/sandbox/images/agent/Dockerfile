# The default sandbox agent image. It provides `sh`, `git` and `claude` on PATH and works
# under any UID:GID: sandbox runs it as the developer's host UID:GID (non-root, all
# capabilities dropped), and the entrypoint adds a passwd/group entry for that ID.
FROM ubuntu:24.04

ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl git less nodejs npm openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --global --no-fund --no-audit --no-update-notifier \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    # Free UID/GID 1000 so a host user with that ID gets the entry the entrypoint adds.
    && userdel --remove ubuntu \
    # The entrypoint runs unprivileged, so it can only add its entry to world-writable
    # files. Harmless here: the agent has no capabilities and no-new-privileges.
    && chmod 0666 /etc/passwd /etc/group \
    # Likewise the system CA bundle, so the entrypoint can add the set's mkcert CA. Only
    # this container trusts what is added, and the agent could trust anything it likes in
    # its own processes anyway.
    && chmod 0666 /etc/ssl/certs/ca-certificates.crt \
    # HOME for whatever UID the container runs as; sandbox mounts ~/.claude inside it.
    && mkdir -p /home/agent /workspace \
    && chmod 1777 /home/agent /workspace

COPY entrypoint.sh /usr/local/bin/sandbox-agent-entrypoint
RUN chmod 0755 /usr/local/bin/sandbox-agent-entrypoint

ENV HOME=/home/agent \
    CLAUDE_CONFIG_DIR=/home/agent/.claude
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/sandbox-agent-entrypoint"]
CMD ["sleep", "infinity"]
