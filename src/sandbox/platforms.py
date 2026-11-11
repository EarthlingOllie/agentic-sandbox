"""Platform gate and the host seam.

sandbox relies on fcntl locking and POSIX tooling (Linux and macOS only). :class:`Host` is
everything sandbox reads about the machine beyond its own config: the platform, the
filesystem root that system files (the hosts helper, its sudoers rule, the launchd job) live
under, how commands run, who runs them and as whom. Production uses the real machine;
tests pass a :class:`Host` rooted in a temp directory with a fake command runner.
"""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sandbox.errors import SandboxError

SUPPORTED_PLATFORMS = ("linux", "darwin")
MACOS: Final = "darwin"
LINUX: Final = "linux"


def ensure_supported_platform(platform: str | None = None) -> None:
    """Fail with a clear message on anything other than Linux or macOS."""
    current = sys.platform if platform is None else platform
    if current.startswith("win") or current == "cygwin":
        raise SandboxError(
            f"unsupported platform {current!r}: sandbox runs on Linux and macOS only "
            "(it needs fcntl file locking, loopback aliases and /etc/hosts)"
        )
    if not current.startswith(SUPPORTED_PLATFORMS):
        raise SandboxError(
            f"unsupported platform {current!r}: sandbox runs on Linux and macOS only"
        )


def current_platform() -> str:
    """``"darwin"`` on macOS, ``"linux"`` otherwise (unsupported ones never get this far)."""
    return MACOS if sys.platform == MACOS else LINUX


# argv -> (exit status, combined output)
Runner = Callable[[Sequence[str]], Awaitable[tuple[int, str]]]


async def run_command(argv: Sequence[str]) -> tuple[int, str]:
    """Run ``argv`` without a shell or stdin; a missing program is exit status 127."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        return 127, str(exc)
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode(errors="replace").strip()


def home_of(user: str) -> Path | None:
    # Imported here: pwd does not exist on Windows, which must reach the platform check.
    import pwd

    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return None


def _geteuid() -> int:
    return os.geteuid()


@dataclass(frozen=True)
class Host:
    """The machine sandbox runs on."""

    # Looked up when a Host is made, not at import, so tests can pin it.
    platform: str = field(default_factory=lambda: current_platform())
    # System paths (/usr/local/bin/..., /etc/sudoers.d/...) are taken relative to this.
    root: Path = Path("/")
    run: Runner = run_command
    euid: int = field(default_factory=_geteuid)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    # The root-owned interpreter the installed hosts helper runs under.
    python: Path = Path("/usr/bin/python3")
    homes: Callable[[str], Path | None] = home_of

    @property
    def is_macos(self) -> bool:
        return self.platform == MACOS

    def path(self, system_path: Path | str) -> Path:
        """``system_path`` (absolute, e.g. ``/etc/hosts``) on this host's filesystem."""
        return self.root / Path(system_path).relative_to("/")
