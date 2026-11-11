"""What ``status`` reports: one model each for the listing and a single set.

The table renderer and ``--json`` print the same models, so scripts see exactly the facts
the tables show (``--json`` is ``model_dump_json``).
"""

from enum import StrEnum
from ipaddress import IPv4Address
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class SetStateName(StrEnum):
    RUNNING = "running"  # every service (and the agent) running
    STOPPED = "stopped"  # a plan, nothing running
    PARTIAL = "partial"  # a plan, some but not all services running
    ORPHANED = "orphaned"  # containers or a lease without a set plan, or a plan-less set dir


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class SetSummary(_Model):
    name: str
    state: SetStateName
    # Why the set is partial or orphaned (empty otherwise).
    detail: str = ""


class StatusList(_Model):
    sets: list[SetSummary]


class ServiceStatus(_Model):
    name: str
    # Docker's container state (running, exited, ...), or "missing" without a container.
    state: str
    health: str | None
    hostname: str | None
    ip: IPv4Address | None


class RepoStatus(_Model):
    name: str
    path: Path
    # False when the agent's clone is gone (or no longer a git repository sandbox can read).
    exists: bool
    branch: str | None  # None when the clone is missing or on a detached HEAD
    modified: bool
    untracked: bool
    # How syncing agent/<set> into the source repo went (``sandbox.git.SyncOutcome``),
    # and why it didn't when it didn't.
    sync: str = "unchanged"
    sync_detail: str = ""


class SetStatus(_Model):
    name: str
    state: SetStateName
    detail: str = ""
    project: str
    services: list[ServiceStatus]
    repos: list[RepoStatus]
