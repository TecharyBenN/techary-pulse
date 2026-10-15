import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from pulse.adapters.store import SqliteStore
from pulse.entities.errors import MailboxError, RunError
from pulse.entrypoints.scheduler import poll, schedule, start
from tests.fakes.mailbox import FakeMailbox
from tests.fakes.models import gateway_error, make_orchestrator, reply_with, responses
from tests.messages import make_newsletter
from tests.operations import OPERATORS, TIMEZONE, make_outbox

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
    assert [(vars(r)["job"], r.exc_info and r.exc_info[0]) for r in records] == [
        ("email", MailboxError),
        ("email", MailboxError),
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


FRIDAY_HALF_FIVE = "30 17 * * FRI"
# 16:30 UTC is 17:30 in London in October, before the clocks change.
SLOT = datetime(2026, 10, 16, 16, 30, tzinfo=UTC)
NEXT_SLOT = SLOT + timedelta(days=7)
TICK = timedelta(milliseconds=50)


class _PassingClock:
    """A clock whose time moves on by one tick at each reading, as real time would."""

    def __init__(self, time: datetime) -> None:
        self.time = time

    def __call__(self) -> datetime:
        now = self.time
        self.time += TICK
        return now


async def test_runs_the_job_at_each_start_of_the_schedule() -> None:
    clock = _PassingClock(SLOT - 2 * TICK)
    stop = asyncio.Event()
    started: list[datetime] = []

    async def job() -> None:
        started.append(clock.time)
        if len(started) == 2:
            stop.set()
        # The week passes until just before the next start.
        clock.time = NEXT_SLOT - 2 * TICK

    await asyncio.wait_for(schedule(FRIDAY_HALF_FIVE, TIMEZONE, clock, job, stop), timeout=2)

    assert len(started) == 2
    assert SLOT <= started[0] < SLOT + timedelta(seconds=1)
    assert NEXT_SLOT <= started[1] < NEXT_SLOT + timedelta(seconds=1)


async def test_stops_while_waiting_without_running_the_job() -> None:
    stop = asyncio.Event()
    ran = False

    async def job() -> None:
        nonlocal ran
        ran = True

    waiting = asyncio.create_task(
        schedule(FRIDAY_HALF_FIVE, TIMEZONE, _PassingClock(SLOT - timedelta(days=1)), job, stop)
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(waiting, timeout=1)

    assert not ran


async def test_a_failed_start_is_logged_and_the_schedule_carries_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    clock = _PassingClock(SLOT - 2 * TICK)
    stop = asyncio.Event()
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1
        clock.time = NEXT_SLOT - 2 * TICK
        if calls == 2:
            stop.set()
            return
        raise RunError("the orchestrator run did not complete")

    await asyncio.wait_for(schedule(FRIDAY_HALF_FIVE, TIMEZONE, clock, job, stop), timeout=2)

    assert calls == 2
    [record] = [r for r in caplog.records if r.getMessage() == "draft_failed"]
    assert record.exc_info is not None and record.exc_info[0] is RunError


async def test_start_posts_the_start_instruction(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    conversation = FakeMailbox()

    await start(
        make_orchestrator(store, reply_with("Drafted.")),
        make_outbox(conversation, store),
        "pulse draft",
    )

    [owner] = {row.message_id for row in await store.load_history("n-1")}
    assert owner.startswith("start-draft-")
    assert conversation.sent == []


async def test_a_failed_start_alerts_the_operators(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    conversation = FakeMailbox()

    with pytest.raises(RunError):
        await start(
            make_orchestrator(store, responses(gateway_error())),
            make_outbox(conversation, store),
            "scheduler",
        )

    [alert] = conversation.sent
    assert alert.to == OPERATORS
    assert alert.subject == "Pulse alert: newsletter not drafted"
