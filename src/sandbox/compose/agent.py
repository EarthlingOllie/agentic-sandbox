"""The agent container: the override fragment that adds a hardened ``agent`` service.

Every set gets one. It runs the bundled default image (``sandbox/images/agent``, built by
Compose on first use and tagged with a hash of its build context, so an updated image is
rebuilt) as the developer's
host UID:GID, never root, with every capability dropped, ``no-new-privileges``, and no
Docker socket. It sees exactly:

- the set directory at ``/workspace/<set>/`` (where shells start);
- each source repo's ``.git`` read-write at its exact host path, because a worktree's
  ``.git`` file points there; the source's working tree itself is never mounted;
- the host's ``~/.claude/`` at ``/home/agent/.claude`` (``CLAUDE_CONFIG_DIR``), so every
  set shares one login. The host's ``~/.claude.json`` is not mounted.
"""

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from sandbox.compose import AGENT_SERVICE, SET_LABEL

AGENT_HOME: Final = "/home/agent"
AGENT_CLAUDE_DIR: Final = f"{AGENT_HOME}/.claude"
WORKSPACE_ROOT: Final = "/workspace"
IMAGE_DIR: Final = Path(__file__).resolve().parent.parent / "images" / "agent"
IMAGE_REPOSITORY: Final = "sandbox-agent"
# Used when sandbox itself runs as root: the agent must never be root.
FALLBACK_USER: Final = "1000:1000"


def workspace(set_name: str) -> str:
    """Where the set directory appears inside the agent."""
    return f"{WORKSPACE_ROOT}/{set_name}"


def default_image() -> str:
    """The default image's tag: content-addressed, so editing the image rebuilds it."""
    digest = hashlib.sha256()
    for path in sorted(p for p in IMAGE_DIR.iterdir() if p.is_file()):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"{IMAGE_REPOSITORY}:{digest.hexdigest()[:12]}"


def agent_user() -> str:
    """The host UID:GID, so files the agent writes stay the developer's."""
    uid, gid = os.getuid(), os.getgid()
    if uid == 0 or gid == 0:
        return FALLBACK_USER
    return f"{uid}:{gid}"


def host_claude_dir() -> Path:
    return Path.home() / ".claude"


def _bind(source: Path, target: str) -> dict[str, Any]:
    return {"type": "bind", "source": str(source), "target": target}


def agent_fragment(set_name: str, set_dir: Path, sources: Mapping[str, Path]) -> dict[str, Any]:
    claude_dir = host_claude_dir()
    # Created here because Docker would otherwise create a missing bind source as root.
    claude_dir.mkdir(parents=True, exist_ok=True)
    volumes = [
        _bind(set_dir, workspace(set_name)),
        *(_bind(sources[name] / ".git", str(sources[name] / ".git")) for name in sorted(sources)),
        _bind(claude_dir, AGENT_CLAUDE_DIR),
    ]
    service: dict[str, Any] = {
        "image": default_image(),
        "build": {"context": str(IMAGE_DIR)},
        # Compose builds the image when it is missing and never pulls it, so an image of
        # the same name on a registry can't stand in for the bundled one.
        "pull_policy": "never",
        "user": agent_user(),
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "init": True,
        "working_dir": workspace(set_name),
        "environment": {"HOME": AGENT_HOME, "CLAUDE_CONFIG_DIR": AGENT_CLAUDE_DIR},
        "volumes": volumes,
        "labels": {SET_LABEL: set_name},
    }
    return {"services": {AGENT_SERVICE: service}}
