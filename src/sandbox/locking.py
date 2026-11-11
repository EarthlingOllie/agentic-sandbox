"""Cross-process advisory file locks (``flock``), usable from asyncio without blocking the loop.

Part of the ``sandbox-hosts`` helper bundle: standard library only, Python 3.9 compatible.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

POLL_INTERVAL_SECONDS = 0.05


@asynccontextmanager
async def file_lock(path: Path) -> AsyncGenerator[None]:
    """Hold an exclusive ``flock`` on ``path`` for the duration of the block.

    Acquisition polls with ``LOCK_NB`` rather than blocking a thread, so waiting is
    cancellable and never leaves a thread holding a lock nobody will release.
    """
    # Imported here, not at module level: fcntl does not exist on Windows, and importing
    # the CLI there must reach the "unsupported platform" check instead of an ImportError.
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
