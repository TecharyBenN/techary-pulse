from datetime import timedelta
from pathlib import Path

import pytest

from pulse.adapters.store import SqliteStore
from pulse.entities.content import CheckFailure, Version
from pulse.entities.errors import MailboxError, Refusal, StoreError
from pulse.entities.lifecycle import Newsletter, start_send
from pulse.entities.mail import OutboundEmail
from pulse.services.operations import Operations
from tests.emails import (
    ENTRY,
    make_consolidation,
    make_draft,
    make_email,
    make_item,
    make_output,
    make_verdict,
    screen_email,
)
from tests.fakes.clock import ControlledClock
from tests.fakes.mailbox import FakeMailbox
from tests.messages import OPENED, make_newsletter
from tests.operations import REVIEWERS, make_operations

PRESENTED = OPENED + timedelta(hours=2)

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
def conversation() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
def operations(
    store: SqliteStore, mailbox: FakeMailbox, conversation: FakeMailbox, clock: ControlledClock
) -> Operations:
    return make_operations(store, mailbox, conversation, clock)


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
    emails = await store.list_screened_emails(result.newsletter_id)
    assert [(s.message_id, s.rejection) for s in emails] == [
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
    emails = await store.list_screened_emails(first.newsletter_id)
    assert [s.message_id for s in emails] == ["m01", "m02", "m13"]


async def test_opens_with_an_empty_inbox(store: SqliteStore, clock: ControlledClock) -> None:
    result = await make_operations(store, clock=clock).start_newsletter()

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


class _FailingMailbox(FakeMailbox):
    """A conversation mailbox whose first send fails, as a Graph error would."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def send(self, email: OutboundEmail) -> str:
        if self.failures:
            self.failures -= 1
            raise MailboxError("Graph returned HTTP 503")
        return await super().send(email)


class _FailingStore(SqliteStore):
    """A store whose first version save fails, as a crash after the send would leave it."""

    failures = 1

    async def save_version(self, newsletter: Newsletter, version: Version) -> None:
        if self.failures:
            self.failures -= 1
            raise StoreError("store operation failed: OperationalError")
        await super().save_version(newsletter, version)


async def _drafted(store: SqliteStore) -> None:
    """An open newsletter with one included email, its item and a working draft."""
    newsletter = make_newsletter()
    await store.save_start(newsletter, [screen_email(make_email("m01"))])
    await store.save_extract("n-1", "m01", make_output())
    await store.save_items("n-1", make_consolidation(make_item(source_message_ids=["m01"])))
    await store.save_draft("n-1", make_draft())


async def _versions(store: SqliteStore) -> list[Version]:
    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    versions = []
    for n in range(1, (newsletter.latest_version or 0) + 1):
        version = await store.get_version("n-1", n)
        assert version is not None
        versions.append(version)
    return versions


async def test_present_saves_the_draft_as_version_1_and_emails_the_reviewers(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox, clock: ControlledClock
) -> None:
    await _drafted(store)
    clock.time = PRESENTED

    result = await operations.present_draft()

    assert result.version == 1
    [version] = await _versions(store)
    assert (version.version, version.created_at) == (1, PRESENTED)
    assert version.content == make_draft().content
    [email] = conversation.sent
    assert (email.to, email.subject, email.reply_to, email.body.content_type) == (
        [REVIEWERS],
        "Draft: Pulse: 25 September 2026",
        None,
        "html",
    )
    assert "Version 1" in email.body.content
    assert ENTRY in email.body.content
    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    # Version 1 starts the newsletter's email thread.
    assert newsletter.thread_message_id == "sent-1"


async def test_later_versions_reply_to_the_latest_message_in_the_thread(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox
) -> None:
    await _drafted(store)
    await operations.present_draft()
    await store.save_draft("n-1", make_draft(changes=["Shortened the intro"]))

    result = await operations.present_draft()
    await operations.present_draft()

    assert result.version == 2
    assert [v.changes for v in await _versions(store)] == [
        [],
        ["Shortened the intro"],
        ["Shortened the intro"],
    ]
    assert len(conversation.sent) == 1
    # Each version replies to the one before it, so the thread reads in order.
    assert [(r.message_id, r.to, r.reply_id) for r in conversation.replies] == [
        ("sent-1", (REVIEWERS,), "reply-1"),
        ("reply-1", (REVIEWERS,), "reply-2"),
    ]
    [second, _] = conversation.replies
    assert "Version 2" in second.body.content
    assert "Shortened the intro" in second.body.content
    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    assert newsletter.thread_message_id == "reply-2"


DASHED = make_draft().model_copy(
    update={
        "content": make_draft().content.model_copy(
            update={"intro": "A strong week \N{EM DASH} again."}
        )
    }
)
DASH_FAILURE = CheckFailure(check="dashes", target="intro", detail="em or en dash")


async def test_check_returns_every_failure_of_the_working_draft(
    operations: Operations, store: SqliteStore
) -> None:
    await _drafted(store)
    assert await operations.check() == []

    await store.save_draft("n-1", DASHED)

    assert await operations.check() == [DASH_FAILURE]


async def test_check_refuses_without_a_working_draft(
    operations: Operations, store: SqliteStore
) -> None:
    await store.save_start(make_newsletter(), [])

    with pytest.raises(Refusal, match="there is no working draft"):
        await operations.check()


async def test_present_saves_the_check_failures_and_the_latest_verdicts(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox
) -> None:
    await _drafted(store)
    await store.save_draft("n-1", DASHED)
    verdicts = [make_verdict("intro"), make_verdict(claim="Signed two customers")]
    await store.save_verdicts("n-1", verdicts)

    await operations.present_draft()

    # A version can be presented with failures; its reviewer email lists them.
    [version] = await _versions(store)
    assert (version.check_failures, version.verdicts) == ([DASH_FAILURE], verdicts)
    [email] = conversation.sent
    assert "Dashes: em or en dash" in email.body.content
    assert "Signed two customers" in email.body.content


async def test_a_draft_rewritten_since_it_was_judged_is_presented_not_judged(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox
) -> None:
    await _drafted(store)
    await store.save_verdicts("n-1", [make_verdict("intro"), make_verdict()])
    await store.save_draft("n-1", make_draft(changes=["Shortened the intro"]))

    await operations.present_draft()

    [version] = await _versions(store)
    assert version.verdicts is None
    [email] = conversation.sent
    assert "This version was not judged." in email.body.content


async def test_present_refuses_without_an_open_newsletter(operations: Operations) -> None:
    with pytest.raises(Refusal, match="no newsletter is open"):
        await operations.present_draft()


async def test_present_refuses_without_a_working_draft(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox
) -> None:
    await store.save_start(make_newsletter(), [])

    with pytest.raises(Refusal, match="there is no working draft"):
        await operations.present_draft()

    assert conversation.sent == []


async def test_present_refuses_once_the_send_has_started(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox
) -> None:
    await _drafted(store)
    approved = make_newsletter().model_copy(
        update={"state": "approved", "approved_version": 1, "latest_version": 1}
    )
    await store.save_start(start_send(approved), [])

    with pytest.raises(Refusal, match="send has started"):
        await operations.present_draft()

    assert conversation.sent == []


async def test_failed_send_saves_nothing_and_the_retry_presents_the_same_version(
    store: SqliteStore,
) -> None:
    await _drafted(store)
    conversation = _FailingMailbox()
    operations = make_operations(store, conversation=conversation)

    with pytest.raises(MailboxError):
        await operations.present_draft()
    assert await _versions(store) == []

    result = await operations.present_draft()

    assert result.version == 1
    assert [e.subject for e in conversation.sent] == ["Draft: Pulse: 25 September 2026"]


async def test_failed_save_after_the_send_resends_the_same_version(tmp_path: Path) -> None:
    store = _FailingStore(tmp_path / "pulse.db")
    await store.initialise()
    await _drafted(store)
    conversation = FakeMailbox()
    operations = make_operations(store, conversation=conversation)

    with pytest.raises(StoreError):
        await operations.present_draft()

    result = await operations.present_draft()

    assert result.version == 1
    assert [e.subject for e in conversation.sent] == [
        "Draft: Pulse: 25 September 2026",
        "Draft: Pulse: 25 September 2026",
    ]
