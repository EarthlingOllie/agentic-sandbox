"""The ``up`` pipeline: ordered steps over a shared :class:`UpContext`, with rollback.

``up`` runs two ordered lists of :class:`Step` (registered in ``sandbox.provisioner``):

1. **Validation steps** check everything that can fail cheaply. They create nothing, so a
   failure here needs no rollback; every one of them finishes before any container starts.
2. **Provision steps** create things in order. If one fails, or ``up`` is cancelled (Ctrl-C),
   the steps that started are rolled back in reverse order. Rollback is shielded from
   cancellation, and Ctrl-C (SIGINT) is ignored while it runs, so a second Ctrl-C cannot
   abort it and leave the set's containers orphaned. If the rollback dies anyway, ``up``
   says so and names the Compose project that may be left.

A step is a name, an async ``run(ctx)`` and an optional async ``rollback(ctx)``. Rollback
undoes the set's own infrastructure only (containers, network, volumes, host entries); plan
and worktrees are kept so rerunning ``up`` resumes. Steps that fan out (one task per repo)
use :func:`fail_fast`, a ``TaskGroup`` that cancels sibling work on the first failure.
"""

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Coroutine, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from sandbox.agent_image import DockerImageProbe, ImageProbe
from sandbox.certs import Mkcert, SetCertificate, SubprocessMkcert
from sandbox.compose.cli import Compose
from sandbox.compose.dedup import ServiceGraph
from sandbox.compose.services import RepoCompose
from sandbox.compose.web import WebApp
from sandbox.config import Config
from sandbox.errors import SandboxError
from sandbox.git import Git
from sandbox.leases import SetLease
from sandbox.plan import SetPlan
from sandbox.platforms import Host
from sandbox.state import SetState, UpAction
from sandbox.worktrees import WorktreeManager, WorktreeResult

if TYPE_CHECKING:
    from sandbox.hosts import HostsWriter

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
    # Writes the set's hosts block; None means the one the config selects.
    hosts: "HostsWriter | None" = None
    mkcert: Mkcert = field(default_factory=SubprocessMkcert)
    host: Host = field(default_factory=Host)
    # Filled in by validation steps.
    repo_composes: list[RepoCompose] = field(default_factory=list[RepoCompose])
    services: list[str] = field(default_factory=list[str])
    graph: ServiceGraph = field(default_factory=lambda: ServiceGraph({}))
    running: set[str] = field(default_factory=set[str])
    state: SetState = SetState.ABSENT
    action: UpAction = UpAction.PROVISION
    # macOS: how many of the pool's /24s have lo0 aliases (None: no limit, as on Linux).
    alias_blocks: int | None = None
    # The set's loopback IP lease (None when it has no services).
    lease: SetLease | None = None
    # The repos' HTTPS web apps, and the mkcert CA certificate (None without a web app).
    web_apps: list[WebApp] = field(default_factory=list[WebApp])
    root_ca: Path | None = None
    # Filled in by provision steps.
    plan_created: bool = False
    worktrees: list[WorktreeResult] = field(default_factory=list[WorktreeResult])
    certificate: SetCertificate | None = None


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
        try:
            with _sigint_ignored():
                problems = await shielded(_roll_back(reversed(started), ctx))
        except BaseException:
            ctx.warn(
                f"rollback of set {ctx.set_name!r} was interrupted: containers, network "
                f"and volumes of Compose project {ctx.project} may be left\n"
                f"clean up with: sandbox {ctx.set_name} down"
            )
            raise
        report = _rollback_report(ctx, problems)
        if isinstance(exc, SandboxError):
            raise SandboxError(f"{exc}\n{report}") from exc
        ctx.warn(report)
        raise


SIGINT_IGNORED_NOTE: Final = b"sandbox: rolling back; Ctrl-C is ignored until it finishes\n"


@contextmanager
def _sigint_ignored() -> Generator[None]:
    """Ignore Ctrl-C while the rollback runs.

    ``asyncio.run`` turns a second SIGINT into a KeyboardInterrupt that no shield stops; it
    would cancel the rollback and kill its ``compose down``. The previous handler is
    restored afterwards, so the first Ctrl-C still ends ``up`` with KeyboardInterrupt.
    """
    try:
        previous = signal.signal(signal.SIGINT, _note_ignored_sigint)
    except ValueError:  # not the main thread: SIGINT is never delivered here
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous if previous is not None else signal.SIG_DFL)


def _note_ignored_sigint(_signum: int, _frame: object) -> None:
    os.write(2, SIGINT_IGNORED_NOTE)  # unbuffered: safe in a signal handler


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
