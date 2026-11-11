"""Override fragment: each repo's init directory, mounted read-only in the services it hooks.

See ``sandbox.hooks`` for discovery and running. Mounts use Compose's long volume syntax;
Compose merges volumes across files by ``target``, so these add to the repo's own volumes.
"""

from collections.abc import Sequence
from typing import Any

from sandbox.hooks import InitHook


def hooks_fragment(hooks: Sequence[InitHook]) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for hook in hooks:
        volumes: list[dict[str, Any]] = services.setdefault(hook.service, {"volumes": []})[
            "volumes"
        ]
        volumes.append(
            {
                "type": "bind",
                "source": str(hook.directory),
                "target": str(hook.mount),
                "read_only": True,
            }
        )
    return {"services": services} if services else {}
