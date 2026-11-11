"""Naming rules for sets and repos, and the verbs reserved by the CLI."""

import re
from typing import Final

from sandbox.errors import SandboxError

# Every verb in the CLI surface is reserved, including ones not implemented yet, so a set
# created today can never be shadowed by a verb added later.
RESERVED_VERBS: Final = frozenset(
    {
        "claude",
        "destroy",
        "down",
        "prune",
        "save",
        "setup",
        "sh",
        "status",
        "templates",
        "up",
        "urls",
    }
)

# A set name becomes a DNS label (`<svc>.<set>.test`) and part of a Compose project name,
# so it is restricted to lowercase letters, digits and inner hyphens.
SET_NAME_PATTERN: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# A repo name is a directory name inside the set directory; no dots keeps it from ever
# colliding with the files sandbox writes there (sandbox.yaml, override.yaml, certificates).
REPO_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

# The set plan, at the root of the set directory.
PLAN_FILENAME: Final = "sandbox.yaml"
BRANCH_PREFIX: Final = "agent/"


def validate_set_name(name: str) -> str:
    if name in RESERVED_VERBS:
        raise SandboxError(
            f"{name!r} is a sandbox verb and cannot be used as a set name "
            f"(reserved: {', '.join(sorted(RESERVED_VERBS))})"
        )
    if not SET_NAME_PATTERN.fullmatch(name):
        raise SandboxError(
            f"invalid set name {name!r}: use lowercase letters, digits and hyphens "
            "(1-63 characters, not starting or ending with a hyphen)"
        )
    return name


def validate_repo_name(name: str) -> str:
    if not REPO_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"invalid repo name {name!r}: use letters, digits, '_' and '-' "
            "(1-63 characters, starting with a letter or digit)"
        )
    return name


def agent_branch(set_name: str) -> str:
    return f"{BRANCH_PREFIX}{set_name}"
