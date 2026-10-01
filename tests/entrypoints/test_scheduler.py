import asyncio
import logging
from datetime import timedelta

import pytest

from pulse.entities.errors import MailboxError
from pulse.entrypoints.scheduler import poll

pytestmark = pytest.mark.anyio


async def test_polls_again_after_a_failed_delivery(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    stop = asyncio.Event()
    calls = 0

    async def deliver() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MailboxError("Graph returned HTTP 503")
        if calls == 3:
            stop.set()

    await poll(deliver, timedelta(0), stop)

    assert calls == 3
    [record] = [r for r in caplog.records if r.getMessage() == "delivery_failed"]
    assert vars(record)["error_type"] == "MailboxError"


async def test_stops_only_between_deliveries() -> None:
    stop = asyncio.Event()
    finished: list[bool] = []

    async def deliver() -> None:
        stop.set()
        await asyncio.sleep(0.01)
        finished.append(True)

    await asyncio.wait_for(poll(deliver, timedelta(hours=1), stop), timeout=1)

    assert finished == [True]
