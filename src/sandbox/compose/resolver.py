"""The resolver's definition: one dnsmasq container in its own Compose project.

The resolver answers lookups of set hostnames from the developer's machine by forwarding
names under the domain to Docker's DNS, which knows every container on the networks the
resolver is attached to. Its image is built from ``sandbox/images/resolver`` (bundled with
sandbox, tagged with a hash of that directory, never pulled). It is labelled as sandbox's
but carries no set label: it belongs to no set, so no set's ``down``, ``prune`` or
``destroy`` removes it. Its Compose project name contains an underscore, which no set name
may, so it can never be a set's project either.

Every set's containers can send it packets, so the bundled definition locks it down: an
unprivileged user, every capability dropped (it listens on an unprivileged port inside the
container, so binding needs none), no-new-privileges, a read-only root filesystem and no
mounts. It restarts with Docker, which puts it back on the networks it was attached to.

The steps that start, attach and detach it are in ``sandbox.resolver``.
"""

import hashlib
from pathlib import Path
from typing import Final

from sandbox.compose import project_name
from sandbox.compose.network import SET_NETWORK

RESOLVER_DIR: Final = Path(__file__).resolve().parent.parent / "images" / "resolver"
COMPOSE_FILE: Final = RESOLVER_DIR / "compose.yaml"
IMAGE_REPOSITORY: Final = "sandbox-resolver"
RESOLVER_PROJECT: Final = "sandbox_resolver"
RESOLVER_CONTAINER: Final = "sandbox_resolver"
# On the resolver's container: marks it as sandbox's (it has no set label).
RESOLVER_LABEL: Final = "sandbox.resolver"
# Where the resolver answers on the developer's machine: loopback only, and not port 53,
# which a local DNS server (systemd-resolved, dnsmasq) may already hold.
RESOLVER_ADDRESS: Final = "127.0.0.1"
RESOLVER_PORT: Final = 15353


def resolver_image() -> str:
    """The resolver image's tag: content-addressed, so editing its definition rebuilds it."""
    digest = hashlib.sha256()
    for path in sorted(p for p in RESOLVER_DIR.iterdir() if p.is_file()):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"{IMAGE_REPOSITORY}:{digest.hexdigest()[:12]}"


def resolver_environment(domain: str) -> dict[str, str]:
    """The variables the bundled compose file interpolates."""
    return {
        "SANDBOX_RESOLVER_IMAGE": resolver_image(),
        "SANDBOX_RESOLVER_CONTAINER": RESOLVER_CONTAINER,
        "SANDBOX_RESOLVER_DOMAIN": domain,
        "SANDBOX_RESOLVER_PORT": str(RESOLVER_PORT),
    }


def set_network(set_name: str) -> str:
    """Docker's name for a set's network, the one the resolver is attached to."""
    return f"{project_name(set_name)}_{SET_NETWORK}"
