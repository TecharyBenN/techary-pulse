"""The scheduler: poll runs the email channel and delivery every poll interval, schedule runs
the draft start on the draft schedule, and start posts the start instruction."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pulse.agents.orchestrator.run import Orchestrator
from pulse.entities.conversation import StartInstruction, StartSource
from pulse.entities.errors import PulseError, RunError
from pulse.entities.lifecycle import next_start
from pulse.services.outbox import Outbox

Job = Callable[[], Awaitable[None]]

_log = logging.getLogger(__name__)


async def poll(jobs: Mapping[str, Job], interval: timedelta, stop: asyncio.Event) -> None:
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
        await _stopped(stop, interval)


async def schedule(
    cron: str,
    timezone: ZoneInfo,
    clock: Callable[[], datetime],
    job: Job,
    stop: asyncio.Event,
) -> None:
    """Run `job` at each start of the draft schedule until `stop` is set; a job in progress is
    always finished. A start missed while Pulse was stopped is not caught up."""
    while True:
        due = next_start(cron, timezone, clock())
        # The clock is read again after each wait, so a wait that ends early waits again.
        while (remaining := due - clock()) > timedelta(0):
            if await _stopped(stop, remaining):
                return
        try:
            await job()
        except PulseError:
            # The next start tries again.
            _log.exception("draft_failed")


async def start(orchestrator: Orchestrator, outbox: Outbox, source: StartSource) -> None:
    """Post the start instruction for `source`, the scheduler or `pulse draft`. Its run has no
    reviewer to see a failure, so a failed run alerts the operators before its error is
    raised."""
    instruction = StartInstruction.new(source)
    try:
        await orchestrator.handle(instruction)
    except RunError:
        await outbox.alert(
            "newsletter not drafted",
            f"The run for start instruction {instruction.message_id} failed. It resumes from its"
            " saved steps at the next reviewer message or start instruction.",
        )
        raise


async def _stopped(stop: asyncio.Event, wait: timedelta) -> bool:
    """Wait for `wait`, or until `stop` is set; return whether it is set."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(wait.total_seconds()):
            await stop.wait()
    return stop.is_set()
