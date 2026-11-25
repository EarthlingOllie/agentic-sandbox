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

# A set name becomes a DNS label (`<svc>.<set>.<domain>`) and part of a Compose project name,
# so it is restricted to lowercase letters, digits and inner hyphens.
SET_NAME_PATTERN: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# A repo name is a directory name inside the set directory; no dots keeps it from ever
# colliding with the files sandbox writes there (sandbox.yaml, override.yaml, certificates).
REPO_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

# The domain every set hostname ends in unless the config says otherwise.
DEFAULT_DOMAIN: Final = "test"
# One lowercase DNS label: letters, digits and inner hyphens.
DNS_LABEL: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# The longest domain that still leaves room for `<service>.<set>.` in a 253-character name.
_MAX_DOMAIN_LENGTH: Final = 125
# mDNS answers every name under this suffix; a resolver for it would fight the system's.
_MDNS_DOMAIN: Final = "local"

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


def validate_domain(domain: str) -> str:
    """``domain`` if set hostnames can end in it: a DNS suffix of one or more lowercase
    labels, not under ``local``. Raises ``ValueError`` (a config field error) otherwise."""
    if (
        not domain
        or len(domain) > _MAX_DOMAIN_LENGTH
        or not all(DNS_LABEL.fullmatch(label) for label in domain.split("."))
    ):
        raise ValueError(
            f"{domain!r} is not a DNS suffix: use one or more dot-separated labels of "
            "lowercase letters, digits and inner hyphens (for example test or sandbox.test)"
        )
    if domain == _MDNS_DOMAIN:
        raise ValueError(f"{domain!r} is reserved for mDNS; choose another domain")
    if domain.endswith(f".{_MDNS_DOMAIN}"):
        raise ValueError(
            f"{domain!r} ends in .{_MDNS_DOMAIN}, which is reserved for mDNS; choose another domain"
        )
    return domain


def validate_repo_name(name: str) -> str:
    if not REPO_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"invalid repo name {name!r}: use letters, digits, '_' and '-' "
            "(1-63 characters, starting with a letter or digit)"
        )
    return name


def agent_branch(set_name: str) -> str:
    return f"{BRANCH_PREFIX}{set_name}"
