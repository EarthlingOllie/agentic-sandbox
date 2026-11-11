"""Everything sandbox does with Docker Compose, one module per concern.

- ``cli``: the subprocess seam around the ``docker compose`` CLI (no Docker SDK).
- ``services``: reading a repo's ``.sandbox/compose.yaml`` and the set-label fragment.
- ``override``: assembling the per-set ``override.yaml`` from registered fragments.

Later concerns (networking, dedup, init hooks, the agent, web apps) add a module here and
register their fragment in ``override.FRAGMENTS``.
"""

from typing import Final

PROJECT_PREFIX: Final = "sandbox-"
SET_LABEL: Final = "sandbox.set"


def project_name(set_name: str) -> str:
    """The Compose project that holds a set's containers, network and volumes."""
    return f"{PROJECT_PREFIX}{set_name}"
