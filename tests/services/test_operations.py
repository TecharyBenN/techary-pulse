from datetime import timedelta
from pathlib import Path

import pytest

from pulse.adapters.store import SqliteStore
from pulse.entities.errors import Refusal
from pulse.entities.lifecycle import start_send
from pulse.services.operations import Operations
from tests.emails import make_email, screen_email
from tests.fakes.clock import ControlledClock
from tests.fakes.mailbox import FakeMailbox
from tests.messages import OPENED

pytestmark = pytest.mark.anyio


@pytest.fixture
async def store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    return store


@pytest.fixture
def mailbox() -> FakeMailbox:
    return FakeMailbox([make_email("m01"), make_email("m13", sender_address="a@example.com")])


@pytest.fixture
def clock() -> ControlledClock:
    return ControlledClock(OPENED)


@pytest.fixture
def operations(store: SqliteStore, mailbox: FakeMailbox, clock: ControlledClock) -> Operations:
    return Operations(store, mailbox, screen_email, clock)


async def test_opens_a_newsletter_with_every_inbox_message(
    operations: Operations, store: SqliteStore
) -> None:
    result = await operations.start_newsletter()

    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    assert (newsletter.newsletter_id, newsletter.state, newsletter.opened_at) == (
        result.newsletter_id,
        "in_review",
        OPENED,
    )
    assert (result.opened, result.added, result.rejected) == (True, 2, 1)
    submissions = await store.list_submissions(result.newsletter_id)
    assert [(s.message_id, s.rejection) for s in submissions] == [
        ("m01", None),
        ("m13", "sender_domain"),
    ]


async def test_adds_only_messages_that_arrived_since(
    operations: Operations, store: SqliteStore, mailbox: FakeMailbox, clock: ControlledClock
) -> None:
    first = await operations.start_newsletter()
    mailbox.inbox.append(make_email("m02"))
    clock.time = OPENED + timedelta(hours=1)

    result = await operations.start_newsletter()

    assert (result.newsletter_id, result.opened, result.added, result.rejected) == (
        first.newsletter_id,
        False,
        1,
        0,
    )
    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    assert (newsletter.opened_at, newsletter.updated_at) == (OPENED, clock.time)
    submissions = await store.list_submissions(first.newsletter_id)
    assert [s.message_id for s in submissions] == ["m01", "m02", "m13"]


async def test_opens_with_an_empty_inbox(store: SqliteStore, clock: ControlledClock) -> None:
    result = await Operations(store, FakeMailbox(), screen_email, clock).start_newsletter()

    assert (result.opened, result.added, result.rejected) == (True, 0, 0)


async def test_refuses_once_the_send_has_started(
    operations: Operations, store: SqliteStore
) -> None:
    await operations.start_newsletter()
    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    started = newsletter.model_copy(
        update={"state": "approved", "approved_version": 1, "latest_version": 1}
    )
    await store.save_start(start_send(started), [])

    with pytest.raises(Refusal, match="send has started"):
        await operations.start_newsletter()
