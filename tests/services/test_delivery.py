import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart

from pulse.adapters.store import SqliteStore
from pulse.entities.errors import MailboxError, Refusal
from pulse.entities.extracts import Sensitivity
from pulse.entities.lifecycle import Newsletter, OnApproval, Scheduled
from pulse.entities.mail import Body, OutboundEmail
from pulse.entities.store import DELIVERY_NOTE
from pulse.services.delivery import Delivery
from pulse.services.operations import Operations
from tests.emails import ENTRY, make_consolidation, make_draft, make_email, make_item, make_output
from tests.fakes.clock import ControlledClock
from tests.fakes.mailbox import FakeMailbox
from tests.messages import OPENED, REVIEWER
from tests.operations import (
    ALL_STAFF,
    OPERATORS,
    SUBMISSIONS,
    make_delivery,
    make_operations,
)

pytestmark = pytest.mark.anyio

APPROVED = OPENED + timedelta(hours=3)
MONDAY_NINE = Scheduled(mode="scheduled", day="MON", time=time(9, 0))
# Opened on Friday 25 September, so the send slot is Monday 28 September at 09:00 in London.
SLOT = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
# m01 is included, m02 excluded, m03 not extracted and m13 rejected by the pre-filter.
INBOX = [
    make_email("m01"),
    make_email("m02", body="Nothing to report."),
    make_email("m03", body="Thanks!"),
    make_email("m13", sender_address="alex.morgan@example.com"),
]


class _Setup:
    def __init__(self, tmp_path: Path, rule: OnApproval | Scheduled) -> None:
        self.store = SqliteStore(tmp_path / "pulse.db")
        self.submissions = FakeMailbox(INBOX)
        self.conversation = FakeMailbox()
        self.clock = ControlledClock(OPENED)
        self.lock = asyncio.Lock()
        self.operations: Operations = make_operations(
            self.store, self.submissions, self.conversation, self.clock, rule
        )
        self.delivery: Delivery = make_delivery(
            self.store, self.submissions, self.conversation, self.clock, self.lock
        )

    async def approved(self, flags: list[Sensitivity] | None = None) -> Newsletter:
        """A newsletter with version 1 presented and approved; `flags` flags m01's entry."""
        await self.store.initialise()
        newsletter_id = (await self.operations.start_newsletter()).newsletter_id
        await self.store.save_extract(newsletter_id, "m01", make_output(sensitivity=flags or []))
        await self.store.save_extract(newsletter_id, "m02", make_output(category=None))
        consolidation = make_consolidation(make_item(source_message_ids=["m01"]))
        await self.store.save_items(newsletter_id, consolidation)
        await self.store.save_draft(newsletter_id, make_draft())
        await self.operations.present_draft()
        self.clock.time = APPROVED
        await self.operations.approve(1, REVIEWER, "approve v1")
        return await self.latest()

    async def latest(self) -> Newsletter:
        newsletter = await self.store.get_latest_newsletter()
        assert newsletter is not None
        return newsletter

    def delivery_through(self, conversation: FakeMailbox) -> Delivery:
        return make_delivery(self.store, self.submissions, conversation, self.clock, self.lock)

    def to_all_staff(self) -> list[OutboundEmail]:
        return [email for email in self.conversation.sent if email.to == [ALL_STAFF]]


@pytest.fixture
def setup(tmp_path: Path) -> _Setup:
    return _Setup(tmp_path, OnApproval(mode="on_approval"))


async def test_sends_the_approved_version_once_to_all_staff(setup: _Setup) -> None:
    await setup.approved()

    await setup.delivery.deliver()
    await setup.delivery.deliver()

    [email] = setup.to_all_staff()
    assert (email.subject, email.reply_to, email.body.content_type) == (
        "Pulse: 25 September 2026",
        SUBMISSIONS,
        "html",
    )
    assert ENTRY in email.body.content
    assert "Review" not in email.body.content
    assert "Version 1" not in email.body.content
    newsletter = await setup.latest()
    assert (newsletter.state, newsletter.send_started, newsletter.sent_at) == (
        "sent",
        True,
        APPROVED,
    )
    assert await setup.store.get_open_newsletter() is None


async def test_a_flagged_version_is_sent_with_its_credit_lines_but_no_flags(
    setup: _Setup,
) -> None:
    newsletter = await setup.approved(
        [Sensitivity(kind="financial", withheld=False, evidence="mentions a price")]
    )
    version = await setup.store.get_version(newsletter.newsletter_id, 1)
    assert version is not None and version.notes.flags

    await setup.delivery.deliver()

    [email] = setup.to_all_staff()
    assert ENTRY in email.body.content
    assert "- Priya Shah" in email.body.content
    assert "Financial" not in email.body.content
    assert "Flagged for review" not in email.body.content
    assert "mentions a price" not in email.body.content


async def test_scheduled_send_waits_for_the_send_slot(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, MONDAY_NINE)
    await setup.approved()

    setup.clock.time = SLOT - timedelta(minutes=1)
    await setup.delivery.deliver()
    assert setup.to_all_staff() == []

    setup.clock.time = SLOT
    await setup.delivery.deliver()
    assert len(setup.to_all_staff()) == 1


async def test_nothing_is_sent_before_approval(setup: _Setup) -> None:
    await setup.approved()
    await setup.operations.withdraw_approval(REVIEWER)

    await setup.delivery.deliver()

    assert setup.to_all_staff() == []


async def test_emails_the_reviewers_that_it_was_sent(setup: _Setup) -> None:
    approved = await setup.approved()

    await setup.delivery.deliver()

    notice = setup.conversation.replies[-1]
    assert notice.message_id == approved.thread_message_id
    assert "Sent" in notice.body.content
    assert (await setup.latest()).thread_message_id == notice.reply_id


async def test_moves_the_screened_emails_and_records_each_move(setup: _Setup) -> None:
    newsletter = await setup.approved()

    await setup.delivery.deliver()

    folders = {
        name: [e.message_id for e in emails] for name, emails in setup.submissions.folders.items()
    }
    assert folders == {"Processed": ["m01", "m02"], "Rejected": ["m13"]}
    # An email the orchestrator did not extract stays pending for the next newsletter.
    assert [e.message_id for e in setup.submissions.inbox] == ["m03"]
    emails = await setup.store.list_screened_emails(newsletter.newsletter_id)
    assert {e.message_id: e.moved for e in emails} == {
        "m01": True,
        "m02": True,
        "m03": False,
        "m13": True,
    }


async def test_notes_the_send_in_the_conversation_history(setup: _Setup) -> None:
    newsletter = await setup.approved()

    await setup.delivery.deliver()

    [row] = await setup.store.load_history(newsletter.newsletter_id)
    assert row.message_id == DELIVERY_NOTE
    [note] = ModelMessagesTypeAdapter.validate_json(row.data)
    assert isinstance(note, ModelRequest)
    [part] = note.parts
    assert isinstance(part, UserPromptPart)
    assert part.content == (
        "Pulse sent version 1 of this newsletter to all staff on 25 September 2026 at 20:30."
    )


class _RepliesFail(FakeMailbox):
    """A conversation mailbox that can send new messages but not reply in a thread."""

    async def reply(self, message_id: str, to: object, body: Body) -> str:
        raise MailboxError("Graph returned HTTP 503")


async def test_a_failed_sent_notice_still_moves_the_emails(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, OnApproval(mode="on_approval"))
    approved = await setup.approved()
    failing = _RepliesFail()
    delivery = setup.delivery_through(failing)

    await delivery.deliver()

    assert [e.to for e in failing.sent] == [[ALL_STAFF]]
    newsletter = await setup.latest()
    assert (newsletter.state, newsletter.thread_message_id) == ("sent", approved.thread_message_id)
    assert set(setup.submissions.folders) == {"Processed", "Rejected"}


async def test_a_failed_send_is_never_repeated(setup: _Setup) -> None:
    await setup.approved()
    setup.conversation.failures = 1

    with pytest.raises(MailboxError):
        await setup.delivery.deliver()
    await setup.delivery.deliver()

    assert setup.to_all_staff() == []
    newsletter = await setup.latest()
    assert (newsletter.state, newsletter.send_started) == ("approved", True)


async def test_a_failed_approval_recheck_alerts_the_operators_and_sends_nothing(
    setup: _Setup, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    approved = await setup.approved()
    # A newer version than the approved one, which code never leaves approved.
    await setup.store.save_newsletter(approved.model_copy(update={"latest_version": 2}))

    await setup.delivery.deliver()

    assert setup.to_all_staff() == []
    [alert] = [e for e in setup.conversation.sent if e.to == OPERATORS]
    assert approved.newsletter_id in alert.body.content
    assert (await setup.latest()).send_started is False


async def test_a_withdrawal_that_takes_the_lock_first_cancels_the_send(setup: _Setup) -> None:
    await setup.approved()

    async with setup.lock:
        # Delivery waits while a run withdraws the approval.
        delivering = asyncio.create_task(setup.delivery.deliver())
        await asyncio.sleep(0)
        await setup.operations.withdraw_approval(REVIEWER)
    await delivering

    assert setup.to_all_staff() == []
    assert (await setup.latest()).state == "in_review"


async def test_a_send_that_takes_the_lock_first_refuses_the_withdrawal(setup: _Setup) -> None:
    await setup.approved()

    delivering = asyncio.create_task(setup.delivery.deliver())
    await asyncio.sleep(0)
    async with setup.lock:
        with pytest.raises(Refusal):
            await setup.operations.withdraw_approval(REVIEWER)
    await delivering

    assert len(setup.to_all_staff()) == 1
    assert (await setup.latest()).state == "sent"
