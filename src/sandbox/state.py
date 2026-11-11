"""The ``up`` state table: what a set directory and Docker say, and what ``up`` does about it.

=================================  =============  ===============
Found                              ``up``         ``up --recreate``
=================================  =============  ===============
no plan (no set directory)         provision      provision
plan, not every service running    resume         resume
plan, every service running        no-op          recreate
=================================  =============  ===============

A worktree-only set (no services) is never "running": resuming it is already a no-op.
"""

from collections.abc import Iterable, Set
from enum import StrEnum


class SetState(StrEnum):
    ABSENT = "absent"
    STOPPED = "stopped"
    RUNNING = "running"


class UpAction(StrEnum):
    PROVISION = "provisioned"
    RESUME = "resumed"
    NOOP = "already running"
    RECREATE = "recreated"


def observe(*, has_plan: bool, expected: Iterable[str], running: Set[str]) -> SetState:
    """Classify a set from whether its plan exists and which of its services are running."""
    if not has_plan:
        return SetState.ABSENT
    wanted = set(expected)
    if wanted and wanted <= running:
        return SetState.RUNNING
    return SetState.STOPPED


def decide(state: SetState, *, recreate: bool) -> UpAction:
    match state:
        case SetState.ABSENT:
            return UpAction.PROVISION
        case SetState.STOPPED:
            return UpAction.RESUME
        case SetState.RUNNING:
            return UpAction.RECREATE if recreate else UpAction.NOOP
