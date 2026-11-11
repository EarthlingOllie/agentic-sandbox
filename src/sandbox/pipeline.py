"""The ``up`` pipeline: ordered steps over a shared :class:`UpContext`, with rollback.

``up`` runs two ordered lists of :class:`Step` (registered in ``sandbox.provisioner``):

1. **Validation steps** check everything that can fail cheaply. They create nothing, so a
   failure here needs no rollback; every one of them finishes before any container starts.
2. **Provision steps** create things in order. If one fails, or ``up`` is cancelled (Ctrl-C),
   the steps that started are rolled back in reverse order. Rollback is shielded from
   cancellation, so a second cancel cannot leave the set's containers orphaned.

A step is a name, an async ``run(ctx)`` and an optional async ``rollback(ctx)``. Rollback
undoes the set's own infrastructure only (containers, network, volumes, host entries); plan
and worktrees are kept so rerunning ``up`` resumes. Steps that fan out (one task per repo)
use :func:`fail_fast`, a ``TaskGroup`` that cancels sibling work on the first failure.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sandbox.agent_image import DockerImageProbe, ImageProbe
from sandbox.compose.cli import Compose
from sandbox.compose.dedup import ServiceGraph
from sandbox.compose.services import RepoCompose
from sandbox.config import Config
from sandbox.errors import SandboxError
from sandbox.git import Git
from sandbox.plan import SetPlan
from sandbox.state import SetState, UpAction
from sandbox.worktrees import WorktreeManager, WorktreeResult

Warn = Callable[[str], None]


@dataclass
class UpContext:
    """Everything one ``up`` knows. Steps read what earlier steps filled in."""

    config: Config
    git: Git
    compose: Compose
    warn: Warn
    worktree_manager: WorktreeManager
    set_name: str
    set_dir: Path
    plan_file: Path
    branch: str
    project: str
    plan: SetPlan
    # True when the plan was already on disk before this `up` started.
    plan_on_disk: bool
    recreate: bool
    images: ImageProbe = field(default_factory=DockerImageProbe)
    # Filled in by validation steps.
    repo_composes: list[RepoCompose] = field(default_factory=list[RepoCompose])
    services: list[str] = field(default_factory=list[str])
    graph: ServiceGraph = field(default_factory=lambda: ServiceGraph({}))
    running: set[str] = field(default_factory=set[str])
    state: SetState = SetState.ABSENT
    action: UpAction = UpAction.PROVISION
    # Filled in by provision steps.
    plan_created: bool = False
    worktrees: list[WorktreeResult] = field(default_factory=list[WorktreeResult])


StepFn = Callable[[UpContext], Awaitable[None]]


@dataclass(frozen=True)
class Step:
    name: str
    run: StepFn
    rollback: StepFn | None = None


async def validate(steps: Sequence[Step], ctx: UpContext) -> None:
    for step in steps:
        await step.run(ctx)


async def provision(steps: Sequence[Step], ctx: UpContext) -> None:
    """Run ``steps`` in order; on failure or cancellation roll back the ones that started."""
    started: list[Step] = []
    try:
        for step in steps:
            started.append(step)
            await step.run(ctx)
    except BaseException as exc:
        problems = await shielded(_roll_back(reversed(started), ctx))
        report = _rollback_report(ctx, problems)
        if isinstance(exc, SandboxError):
            raise SandboxError(f"{exc}\n{report}") from exc
        ctx.warn(report)
        raise


async def _roll_back(steps: Iterable[Step], ctx: UpContext) -> list[str]:
    problems: list[str] = []
    for step in steps:
        if step.rollback is None:
            continue
        try:
            await step.rollback(ctx)
        except Exception as exc:  # every step gets its chance to undo, whatever failed before
            problems.append(f"{step.name}: {exc}")
    return problems


def _rollback_report(ctx: UpContext, problems: list[str]) -> str:
    if problems:
        return (
            f"rollback of set {ctx.set_name!r} was incomplete:\n  "
            + "\n  ".join(problems)
            + f"\nclean up with: sandbox {ctx.set_name} down"
        )
    return (
        f"rolled back set {ctx.set_name!r}: its services were removed; plan and worktrees "
        f"were kept, so `sandbox {ctx.set_name} up` resumes"
    )


async def shielded[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion even if the caller is cancelled (any number of times)."""
    task = asyncio.ensure_future(coro)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            # The caller was cancelled, not the rollback: keep waiting for it to finish.


async def fail_fast[T](coros: Iterable[Coroutine[Any, Any, T]]) -> list[T]:
    """Run ``coros`` concurrently; the first failure cancels the rest (structured concurrency).

    Failures are reported as one :class:`SandboxError` joining every failure's message.
    """
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(coro) for coro in coros]
    except* SandboxError as failures:
        raise SandboxError(_join_messages(failures)) from failures
    return [task.result() for task in tasks]


def _join_messages(group: BaseExceptionGroup[SandboxError]) -> str:
    messages: list[str] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            messages.append(_join_messages(exc))  # pyright: ignore[reportUnknownArgumentType]
        else:
            messages.append(str(exc))
    return "\n".join(messages)
