"""The scheduler: poll runs the email channel and delivery every poll interval."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta

from pulse.entities.errors import PulseError

_log = logging.getLogger(__name__)


async def poll(
    jobs: Mapping[str, Callable[[], Awaitable[None]]], interval: timedelta, stop: asyncio.Event
) -> None:
    """Run each job in order every interval until `stop` is set; a job in progress is always
    finished."""
    while not stop.is_set():
        for name, job in jobs.items():
            if stop.is_set():
                return
            try:
                await job()
            except PulseError:
                # The next poll tries again.
                _log.exception("poll_failed", extra={"job": name})
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval.total_seconds()):
                await stop.wait()
