from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from pulse.adapters.store import SqliteStore
from pulse.entities.content import CheckFailure, Version
from pulse.entities.errors import MailboxError, Refusal, StoreError
from pulse.entities.lifecycle import Newsletter, Scheduled, abandon, start_send
from pulse.services.operations import ApproveResult, NoticeResult, Operations, PresentResult
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
from tests.messages import OPENED, REVIEWER, make_newsletter
from tests.operations import REVIEWERS, TIMEZONE, make_operations

PRESENTED = OPENED + timedelta(hours=2)
MONDAY_NINE = Scheduled(mode="scheduled", day="MON", time=time(9, 0))

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
    conversation = FakeMailbox()
    conversation.failures = 1
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


APPROVED = PRESENTED + timedelta(hours=1)


async def _presented(operations: Operations, store: SqliteStore) -> None:
    """An open newsletter with version 1 presented to the reviewers."""
    await _drafted(store)
    await operations.present_draft()


async def _approved(
    operations: Operations, store: SqliteStore, clock: ControlledClock
) -> ApproveResult:
    await _presented(operations, store)
    clock.time = APPROVED
    return await operations.approve(1, REVIEWER, "approve v1")


async def _newsletter(store: SqliteStore) -> Newsletter:
    newsletter = await store.get_latest_newsletter()
    assert newsletter is not None
    return newsletter


async def test_approve_records_the_approval_with_a_send_time_of_now(
    operations: Operations, store: SqliteStore, clock: ControlledClock
) -> None:
    result = await _approved(operations, store, clock)

    newsletter = await _newsletter(store)
    assert (newsletter.state, newsletter.approved_version, newsletter.approver) == (
        "approved",
        1,
        REVIEWER,
    )
    assert newsletter.approved_at == newsletter.send_time == APPROVED
    assert result == ApproveResult(version=1, send_time=APPROVED.astimezone(TIMEZONE))


async def test_approve_in_scheduled_mode_sends_in_the_send_slot(
    store: SqliteStore, mailbox: FakeMailbox, conversation: FakeMailbox, clock: ControlledClock
) -> None:
    operations = make_operations(store, mailbox, conversation, clock, send_rule=MONDAY_NINE)

    result = await _approved(operations, store, clock)

    # Opened on Friday 25 September, so the slot is Monday 28 September at 09:00 in London.
    assert result.send_time == datetime(2026, 9, 28, 9, 0, tzinfo=TIMEZONE)
    assert result.send_time.tzinfo == TIMEZONE
    assert (await _newsletter(store)).send_time == datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("version", "caller", "message", "reason"),
    [
        (2, REVIEWER, "approve v2", "not the latest presented version"),
        (1, REVIEWER, "Looks good", "does not start with approve v1"),
        (1, None, "approve v1", "no reviewer is present"),
    ],
)
async def test_approve_refuses_and_changes_nothing(
    operations: Operations,
    store: SqliteStore,
    version: int,
    caller: str | None,
    message: str,
    reason: str,
) -> None:
    await _presented(operations, store)
    before = await _newsletter(store)

    with pytest.raises(Refusal, match=reason):
        await operations.approve(version, caller, message)

    assert await _newsletter(store) == before


async def test_approve_refuses_an_approved_newsletter(
    operations: Operations, store: SqliteStore, clock: ControlledClock
) -> None:
    await _approved(operations, store, clock)

    with pytest.raises(Refusal, match="approved, not in review"):
        await operations.approve(1, REVIEWER, "approve v1")


async def test_withdraw_returns_to_review_and_emails_send_cancelled(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox, clock: ControlledClock
) -> None:
    await _approved(operations, store, clock)

    result = await operations.withdraw_approval(REVIEWER)

    assert result == NoticeResult(notice_sent=True)
    newsletter = await _newsletter(store)
    assert (newsletter.state, newsletter.approved_version, newsletter.send_time) == (
        "in_review",
        None,
        None,
    )
    [notice] = conversation.replies
    assert notice.message_id == "sent-1"
    assert "Send cancelled" in notice.body.content
    assert newsletter.thread_message_id == notice.reply_id


async def test_withdraw_reports_a_notice_that_could_not_be_sent(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox, clock: ControlledClock
) -> None:
    await _approved(operations, store, clock)
    conversation.failures = 1

    result = await operations.withdraw_approval(REVIEWER)

    assert result == NoticeResult(notice_sent=False)
    newsletter = await _newsletter(store)
    # The withdrawal stands, and the thread's latest message is unchanged.
    assert (newsletter.state, newsletter.thread_message_id) == ("in_review", "sent-1")


async def test_withdraw_refuses(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox, clock: ControlledClock
) -> None:
    await _presented(operations, store)
    with pytest.raises(Refusal, match="not approved"):
        await operations.withdraw_approval(REVIEWER)

    clock.time = APPROVED
    await operations.approve(1, REVIEWER, "approve v1")
    with pytest.raises(Refusal, match="no reviewer is present"):
        await operations.withdraw_approval(None)

    await store.save_newsletter(start_send(await _newsletter(store)))
    with pytest.raises(Refusal, match="send has started"):
        await operations.withdraw_approval(REVIEWER)
    assert conversation.replies == []


async def test_abandon_closes_the_newsletter_and_emails_abandoned(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox, clock: ControlledClock
) -> None:
    await _presented(operations, store)
    clock.time = APPROVED

    result = await operations.abandon(REVIEWER)

    assert result == NoticeResult(notice_sent=True)
    newsletter = await _newsletter(store)
    assert (newsletter.state, newsletter.closed_at) == ("abandoned", APPROVED)
    assert await store.get_open_newsletter() is None
    [notice] = conversation.replies
    assert "Abandoned" in notice.body.content
    assert newsletter.thread_message_id == notice.reply_id


async def test_abandon_before_any_version_starts_the_thread(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox
) -> None:
    await store.save_start(make_newsletter(), [])

    await operations.abandon(REVIEWER)

    [email] = conversation.sent
    assert email.subject == "Abandoned: Pulse: 25 September 2026"


async def test_abandon_refuses(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox, clock: ControlledClock
) -> None:
    with pytest.raises(Refusal, match="no newsletter is open"):
        await operations.abandon(REVIEWER)

    await _approved(operations, store, clock)
    with pytest.raises(Refusal, match="no reviewer is present"):
        await operations.abandon(None)

    await store.save_newsletter(start_send(await _newsletter(store)))
    with pytest.raises(Refusal, match="send has started"):
        await operations.abandon(REVIEWER)
    assert conversation.replies == []


async def test_present_over_an_approval_withdraws_it_first(
    operations: Operations, store: SqliteStore, conversation: FakeMailbox, clock: ControlledClock
) -> None:
    await _approved(operations, store, clock)
    await store.save_draft("n-1", make_draft(changes=["Shortened the intro"]))

    result = await operations.present_draft()

    assert result == PresentResult(version=2, approval_withdrawn=True, notice_sent=True)
    newsletter = await _newsletter(store)
    assert (newsletter.state, newsletter.latest_version, newsletter.approved_version) == (
        "in_review",
        2,
        None,
    )
    # The notice comes first, then the new version replies to it.
    notice, presented = conversation.replies
    assert "Send cancelled" in notice.body.content
    assert (presented.message_id, "Version 2" in presented.body.content) == (notice.reply_id, True)


async def test_present_without_an_approval_withdraws_nothing(
    operations: Operations, store: SqliteStore
) -> None:
    await _drafted(store)

    assert await operations.present_draft() == PresentResult(version=1)


async def test_check_works_on_a_closed_newsletter(
    operations: Operations, store: SqliteStore
) -> None:
    await _drafted(store)
    await store.save_draft("n-1", DASHED)
    await store.save_newsletter(abandon(make_newsletter(), REVIEWER, OPENED))

    assert await operations.check() == [DASH_FAILURE]
