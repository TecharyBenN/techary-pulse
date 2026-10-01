import logging
from pathlib import Path

import pytest

from pulse.adapters.store import SqliteStore
from pulse.entities.errors import MailboxError
from pulse.entities.mail import Body
from pulse.services.mail import Outbox
from tests.fakes.mailbox import FakeMailbox
from tests.messages import make_newsletter
from tests.operations import OPERATORS, REVIEWERS, make_outbox

pytestmark = pytest.mark.anyio

BODY = Body(content="<p>Version 1</p>", content_type="html")


@pytest.fixture
def mailbox() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
async def store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    await store.save_start(make_newsletter(), [])
    return store


@pytest.fixture
def outbox(mailbox: FakeMailbox, store: SqliteStore) -> Outbox:
    return make_outbox(mailbox, store)


async def test_the_first_email_starts_the_thread_with_a_prefixed_subject(
    outbox: Outbox, mailbox: FakeMailbox
) -> None:
    sent_id = await outbox.to_reviewers(make_newsletter(), BODY, "Draft:")

    [email] = mailbox.sent
    assert (email.to, email.subject, email.body, email.reply_to) == (
        [REVIEWERS],
        "Draft: Pulse: 25 September 2026",
        BODY,
        None,
    )
    assert sent_id == "sent-1"


async def test_later_emails_reply_to_the_latest_message_in_the_thread(
    outbox: Outbox, mailbox: FakeMailbox
) -> None:
    newsletter = make_newsletter().model_copy(update={"thread_message_id": "sent-1"})

    reply_id = await outbox.to_reviewers(newsletter, BODY, "Draft:")

    [reply] = mailbox.replies
    assert (reply.message_id, reply.to, reply.body) == ("sent-1", (REVIEWERS,), BODY)
    assert reply_id == "reply-1"
    assert mailbox.sent == []


async def test_a_threaded_email_becomes_the_thread_latest_message(
    outbox: Outbox, mailbox: FakeMailbox, store: SqliteStore
) -> None:
    newsletter = make_newsletter().model_copy(update={"thread_message_id": "sent-1"})

    saved = await outbox.thread(newsletter, BODY, "Draft:")

    [reply] = mailbox.replies
    assert (reply.message_id, reply.to, reply.body) == ("sent-1", (REVIEWERS,), BODY)
    assert saved.thread_message_id == "reply-1"
    assert await store.get_latest_newsletter() == saved


async def test_a_threaded_email_that_fails_raises_and_changes_nothing(
    outbox: Outbox, mailbox: FakeMailbox, store: SqliteStore
) -> None:
    mailbox.failures = 1

    with pytest.raises(MailboxError):
        await outbox.thread(make_newsletter(), BODY, "Draft:")

    assert await store.get_latest_newsletter() == make_newsletter()


async def test_a_reply_to_a_message_goes_to_the_reviewers_only(
    outbox: Outbox, mailbox: FakeMailbox, store: SqliteStore
) -> None:
    assert await outbox.reply_to_message("c01", BODY) == "reply-1"

    [reply] = mailbox.replies
    assert (reply.message_id, reply.to, reply.body) == ("c01", (REVIEWERS,), BODY)
    # Outside the newsletter's thread, so the thread is unchanged.
    assert await store.get_latest_newsletter() == make_newsletter()


async def test_a_notice_replies_in_the_thread_and_becomes_its_latest_message(
    outbox: Outbox, mailbox: FakeMailbox, store: SqliteStore
) -> None:
    newsletter = make_newsletter().model_copy(update={"thread_message_id": "sent-1"})

    saved, sent = await outbox.notice(newsletter, "Sent", "Version 1 was sent to all staff.")

    [reply] = mailbox.replies
    assert reply.message_id == "sent-1"
    assert reply.body.content_type == "html"
    assert "Version 1 was sent to all staff." in reply.body.content
    assert (sent, saved.thread_message_id) == (True, "reply-1")
    assert await store.get_latest_newsletter() == saved


async def test_a_notice_without_a_thread_starts_one(outbox: Outbox, mailbox: FakeMailbox) -> None:
    await outbox.notice(make_newsletter(), "Abandoned", "This newsletter was abandoned.")

    [email] = mailbox.sent
    assert email.subject == "Abandoned: Pulse: 25 September 2026"


async def test_a_failed_notice_is_logged_and_changes_nothing(
    outbox: Outbox, mailbox: FakeMailbox, store: SqliteStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    mailbox.failures = 1

    assert await outbox.notice(make_newsletter(), "Sent", "Version 1 was sent.") == (
        make_newsletter(),
        False,
    )
    assert await store.get_latest_newsletter() == make_newsletter()

    [record] = [r for r in caplog.records if r.getMessage() == "notice_failed"]
    assert (vars(record)["conversation_id"], vars(record)["error_type"]) == ("n-1", "MailboxError")


async def test_an_alert_goes_to_the_operators_in_plain_text(
    outbox: Outbox, mailbox: FakeMailbox
) -> None:
    await outbox.alert("send not started", "Newsletter n-1 was not sent.")

    [email] = mailbox.sent
    assert (email.to, email.subject, email.body) == (
        OPERATORS,
        "Pulse alert: send not started",
        Body(content="Newsletter n-1 was not sent.", content_type="text"),
    )


async def test_a_failed_alert_is_logged(
    outbox: Outbox, mailbox: FakeMailbox, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    mailbox.failures = 1

    await outbox.alert("send not started", "Newsletter n-1 was not sent.")

    [record] = [r for r in caplog.records if r.getMessage() == "alert_failed"]
    assert vars(record)["error_type"] == "MailboxError"
    assert "n-1 was not sent" not in caplog.text
