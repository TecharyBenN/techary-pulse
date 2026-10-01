"""The scheduler: runs the email channel's poll and delivery every poll interval inside
`pulse serve`."""

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
            except PulseError as error:
                # The next poll tries again; the failure table says what each failure leaves.
                _log.error("poll_failed", extra={"job": name, "error_type": type(error).__name__})
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval.total_seconds()):
                await stop.wait()
