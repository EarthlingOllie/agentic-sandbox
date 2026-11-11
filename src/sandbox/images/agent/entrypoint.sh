#!/bin/sh
# Give the container's UID:GID a name and a home, then run the command.
#
# sandbox starts the agent as the developer's host UID:GID, which the image cannot know in
# advance. Tools such as git and ssh look the current user up in /etc/passwd, so add an
# entry (and a group) when there is none. /etc/passwd and /etc/group are world-writable
# in the image for exactly this; nothing here needs privileges.
set -eu

uid="$(id -u)"
gid="$(id -g)"
home="${HOME:-/home/agent}"

if ! grep -q "^[^:]*:[^:]*:${gid}:" /etc/group; then
    echo "agent:x:${gid}:" >>/etc/group
fi
if ! grep -q "^[^:]*:[^:]*:${uid}:" /etc/passwd; then
    echo "agent:x:${uid}:${gid}:sandbox agent:${home}:/bin/sh" >>/etc/passwd
fi
mkdir -p "$home" 2>/dev/null || true

# In a set with HTTPS web apps, sandbox mounts the machine's mkcert CA certificate (never
# its key) here. Trust it system-wide (curl, git, openssl), once per container.
ca=/sandbox/ca/rootCA.pem
bundle=/etc/ssl/certs/ca-certificates.crt
if [ -r "$ca" ] && [ -w "$bundle" ] && ! grep -qxF "$(sed -n 2p "$ca")" "$bundle"; then
    cat "$ca" >>"$bundle"
fi

exec "$@"
