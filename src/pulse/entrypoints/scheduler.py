"""The scheduler: runs delivery every poll interval inside `pulse serve`."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from pulse.entities.errors import PulseError

_log = logging.getLogger(__name__)


async def poll(
    deliver: Callable[[], Awaitable[None]], interval: timedelta, stop: asyncio.Event
) -> None:
    """Deliver every interval until `stop` is set; a delivery in progress is always finished."""
    while not stop.is_set():
        try:
            await deliver()
        except PulseError as error:
            # The next poll tries again; the failure table says what each failure leaves.
            _log.error("delivery_failed", extra={"error_type": type(error).__name__})
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval.total_seconds()):
                await stop.wait()
