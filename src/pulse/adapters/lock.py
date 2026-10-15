"""The run lock: RunLock lets one orchestrator run or delivery change a newsletter at a time,
across every Pulse process that shares the store."""

import asyncio
import fcntl
import os
from pathlib import Path
from types import TracebackType

# How often a process waiting for another's lock tries again.
_RETRY_SECONDS = 0.1


class RunLock:
    """Taken by orchestrator runs and delivery in `pulse serve`, and by `pulse draft` beside it.

    Within a process, an asyncio lock keeps arrival order, because it wakes its waiters first in,
    first out. Across processes, a lock on a file beside the store is tried without blocking, so a
    cancelled wait never leaves a thread holding it.
    """

    def __init__(self, path: Path) -> None:
        """`path` is the lock file, in the store's directory."""
        self._path = path
        self._in_process = asyncio.Lock()
        self._fd: int | None = None

    async def __aenter__(self) -> None:
        await self._in_process.acquire()
        try:
            self._fd = await self._across_processes()
        except BaseException:
            self._in_process.release()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            # Closing the file releases its lock.
            os.close(fd)
        self._in_process.release()

    async def _across_processes(self) -> int:
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    await asyncio.sleep(_RETRY_SECONDS)
        except BaseException:
            os.close(fd)
            raise
