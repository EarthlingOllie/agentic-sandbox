"""Init hooks: a repo's ``.sandbox/init/<service>.sh``, run inside that (possibly shared) service.

A repo creates its own database, users or seed data in a service by shipping a hook named
after the service. Sandbox mounts each repo's ``.sandbox/init/`` read-only at
``/sandbox/init/<repo>/`` in the services it has hooks for (``sandbox.compose.hooks``) and,
once ``compose up --wait`` reports every service healthy, runs each hook with
``docker compose exec`` in repo-name order. Hooks run on every ``up`` that starts the set
(provision, resume, recreate), so they must be idempotent. The first failing hook fails the
set, which rolls it back; its output is part of the error.

Hooks are read from the repo's source, like its compose file.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from sandbox.compose.services import SANDBOX_DIRNAME
from sandbox.errors import SandboxError
from sandbox.pipeline import UpContext

INIT_DIRNAME: Final = "init"
HOOK_SUFFIX: Final = ".sh"
MOUNT_ROOT: Final = PurePosixPath("/sandbox/init")


@dataclass(frozen=True, order=True)
class InitHook:
    # Field order is run order: repo name first, then service.
    repo: str
    service: str
    # The repo's init directory on the host (mounted as a whole).
    directory: Path

    @property
    def mount(self) -> PurePosixPath:
        """Where ``directory`` is mounted in the service."""
        return MOUNT_ROOT / self.repo

    @property
    def script(self) -> PurePosixPath:
        return self.mount / f"{self.service}{HOOK_SUFFIX}"

    @property
    def label(self) -> str:
        return f"{self.repo}/{SANDBOX_DIRNAME}/{INIT_DIRNAME}/{self.service}{HOOK_SUFFIX}"


def init_dir(source: Path) -> Path:
    return source / SANDBOX_DIRNAME / INIT_DIRNAME


def find_hooks(sources: Mapping[str, Path], services: Collection[str]) -> list[InitHook]:
    """Every repo's hooks, in run order; a hook for a service the set lacks is an error."""
    hooks: list[InitHook] = []
    errors: list[str] = []
    for repo, source in sorted(sources.items()):
        directory = init_dir(source)
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix != HOOK_SUFFIX or not path.is_file():
                continue
            service = path.stem
            if service not in services:
                known = ", ".join(sorted(services)) or "none"
                errors.append(
                    f"{repo}: init hook {SANDBOX_DIRNAME}/{INIT_DIRNAME}/{path.name} targets "
                    f"service {service!r}, which the set does not run (services: {known})"
                )
                continue
            hooks.append(InitHook(repo, service, directory))
    if errors:
        raise SandboxError("\n".join(errors))
    return sorted(hooks)


async def check_hooks(ctx: UpContext) -> None:
    """Validation step: every hook targets one of the set's services."""
    find_hooks(ctx.plan.sources(), ctx.services)


async def run_hooks(ctx: UpContext) -> None:
    """Provision step (after ``compose up --wait``): run each hook; the first failure raises."""
    for hook in find_hooks(ctx.plan.sources(), ctx.services):
        status, output = await ctx.compose.exec_captured(
            ctx.project, hook.service, ["sh", str(hook.script)]
        )
        if status != 0:
            shown = "\n".join(f"  | {line}" for line in output.rstrip().splitlines())
            raise SandboxError(
                f"init hook {hook.label} failed in service {hook.service!r} (exit {status})"
                + (f":\n{shown}" if shown else " with no output")
            )
