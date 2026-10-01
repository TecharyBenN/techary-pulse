import asyncio
import logging
from datetime import timedelta

import pytest

from pulse.entities.errors import MailboxError
from pulse.entrypoints.scheduler import poll

pytestmark = pytest.mark.anyio


async def test_runs_each_job_in_order_every_poll() -> None:
    stop = asyncio.Event()
    calls: list[str] = []

    async def email() -> None:
        calls.append("email")

    async def deliver() -> None:
        calls.append("delivery")
        if len(calls) == 4:
            stop.set()

    await poll({"email": email, "delivery": deliver}, timedelta(0), stop)

    assert calls == ["email", "delivery", "email", "delivery"]


async def test_a_failed_job_is_logged_and_the_others_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    stop = asyncio.Event()
    delivered = 0

    async def email() -> None:
        raise MailboxError("Graph returned HTTP 503")

    async def deliver() -> None:
        nonlocal delivered
        delivered += 1
        if delivered == 2:
            stop.set()

    await poll({"email": email, "delivery": deliver}, timedelta(0), stop)

    assert delivered == 2
    records = [r for r in caplog.records if r.getMessage() == "poll_failed"]
    assert [(vars(r)["job"], vars(r)["error_type"]) for r in records] == [
        ("email", "MailboxError"),
        ("email", "MailboxError"),
    ]


async def test_stops_only_between_jobs() -> None:
    stop = asyncio.Event()
    finished: list[str] = []

    async def email() -> None:
        stop.set()
        await asyncio.sleep(0.01)
        finished.append("email")

    async def deliver() -> None:
        finished.append("delivery")

    jobs = {"email": email, "delivery": deliver}
    await asyncio.wait_for(poll(jobs, timedelta(hours=1), stop), timeout=1)

    assert finished == ["email"]
