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
    ORPHANED = "orphaned"  # containers without a set plan, or a plan-less set directory


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
    # The set hostname: the service's stable address, running or not (the agent's is
    # ``agent.<set>.<domain>``).
    hostname: str | None = None
    # The container's current address, read from Docker; absent unless it is running. It
    # changes between `up`s: connect to the hostname.
    ip: IPv4Address | None = None


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
