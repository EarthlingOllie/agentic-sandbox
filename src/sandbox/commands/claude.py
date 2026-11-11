"""``sandbox SET claude``: run Claude Code interactively in the set's agent container.

Every set's agent mounts the host's ``~/.claude/`` as its ``CLAUDE_CONFIG_DIR``, so all sets
share one login. In the (Linux) container Claude Code keeps its token in plaintext in
``.credentials.json`` there, even when the host is a Mac that keeps its own login in the
Keychain. When that file is missing, the first run tells the user that Claude Code will ask
for ``/login`` and that the login will be shared; afterwards the file is kept owner-only.
"""

import asyncio
import stat
from pathlib import Path
from typing import Final

import typer

from sandbox.agent import ExecTarget, agent_target, load_plan, run_in
from sandbox.compose import AGENT_SERVICE
from sandbox.compose.agent import host_claude_dir
from sandbox.compose.cli import Compose
from sandbox.config import load_config
from sandbox.context import current_set, failure, get_deps
from sandbox.errors import SandboxError

CREDENTIALS_FILE: Final = ".credentials.json"
CREDENTIALS_MODE: Final = 0o600


def _credentials() -> Path:
    return host_claude_dir() / CREDENTIALS_FILE


def _restrict(credentials: Path) -> None:
    """Keep the plaintext token readable by its owner only."""
    try:
        if stat.S_IMODE(credentials.stat().st_mode) != CREDENTIALS_MODE:
            credentials.chmod(CREDENTIALS_MODE)
    except FileNotFoundError:
        pass  # the user quit without logging in


async def _run_claude(compose: Compose, set_name: str, target: ExecTarget) -> int:
    if AGENT_SERVICE not in await compose.running_services(target.project):
        raise SandboxError(
            f"set {set_name!r} is not running (its agent container is not up); "
            f"start it with: sandbox {set_name} up"
        )
    credentials = _credentials()
    if not credentials.exists():
        typer.echo(
            f"No Claude login in {host_claude_dir()} yet: Claude Code will ask you to log in "
            "(or type /login). The login is saved there and shared by every set.",
            err=True,
        )
    try:
        return await run_in(compose, target, ["claude"])
    finally:
        _restrict(credentials)


def claude(ctx: typer.Context) -> None:
    """Run Claude Code in the set's agent, starting in /workspace/SET."""
    set_name = current_set(ctx, "claude")
    try:
        plan = load_plan(load_config().sets_root, set_name)
        target = agent_target(set_name, plan)
        status = asyncio.run(_run_claude(get_deps(ctx).compose, set_name, target))
    except SandboxError as exc:
        failure(exc)
    raise typer.Exit(code=status)
