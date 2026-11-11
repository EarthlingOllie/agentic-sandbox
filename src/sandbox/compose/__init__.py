"""Everything sandbox does with Docker Compose, one module per concern.

- ``cli``: the subprocess seam around the ``docker compose`` CLI (no Docker SDK).
- ``services``: reading a repo's ``.sandbox/compose.yaml`` and the set-label fragment.
- ``override``: assembling the per-set ``override.yaml`` from registered fragments.
- ``dedup``: one instance per same-named service across repos, or a conflict diff.
- ``network``: each service's IP-bound native ports and ``<service>.<set>.test`` aliases.
- ``web``: HTTPS web apps (443, the unprivileged-port sysctl, TLS and CA mounts).

Later concerns (networking, dedup, init hooks, the agent, web apps) add a module here and
register their fragment in ``override.FRAGMENTS``.
"""

from typing import Final

PROJECT_PREFIX: Final = "sandbox-"
SET_LABEL: Final = "sandbox.set"
# Every set's agent container; repos may not declare a service of this name.
AGENT_SERVICE: Final = "agent"


def project_name(set_name: str) -> str:
    """The Compose project that holds a set's containers, network and volumes."""
    return f"{PROJECT_PREFIX}{set_name}"
