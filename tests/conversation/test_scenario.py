"""End to end: build, feedback, revision, approval, withdrawal, re-approval and the send."""

from datetime import datetime

import pytest

from pulse.config import Config, SendConfig
from pulse.conversation.email import poll_once
from pulse.conversation.runner import ConversationRunner
from pulse.periodic import run_periodic_check
from pulse.pipeline.runner import run_build
from pulse.store import EditionStore

from ..support import REVIEWER, FakeMailbox, at, calling, message, scripted
from .conftest import REVISION, build_stand_ins, build_submission

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def test_full_conversation_ends_in_exactly_one_all_staff_send(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    config = config.model_copy(update={"send": SendConfig(mode="on_approval")})
    submissions = build_submission()
    clock = Clock(at(22))
    chat = calling(
        ("revise_draft", {"instruction": "Tidy the wording"}),
        "Version 2 saved.",
        ("approve", {"version": 2}),
        "Version 2 approved.",
        ("withdraw_approval", {}),
        "Approval withdrawn.",
        ("approve", {"version": 2}),
        "Version 2 approved again.",
    )
    agents = build_stand_ins(config, chat=chat, reviser=scripted([REVISION]))
    runner = ConversationRunner(config, store, submissions, conversation, agents, clock, run_build)

    async def reviewer_says(message_id: str, body: str, now: datetime) -> None:
        clock.now = now
        conversation.inbox[message_id] = message(id=message_id, sender_address=REVIEWER, body=body)
        await poll_once(config, conversation, store, runner)

    # 1. Build: version 1 is emailed to reviewers.
    build = await run_build(config, submissions, conversation, store, agents, clock, "command")
    assert build.edition is not None
    assert len(conversation.sent) == 1

    # 2. Feedback by email: the reply-all carries version 2.
    await reviewer_says("m2", "Please tidy the wording.", at(23))
    assert (await store.current_version(build.edition.id)).number == 2
    assert "tidied up" in conversation.replies[-1].html

    # 3. Approval.
    await reviewer_says("m3", "This looks great, approve v2.", at(24))
    edition = await store.open_edition()
    assert edition is not None and edition.state == "approved"

    # 4. Withdrawal, with a notice to reviewers.
    await reviewer_says("m4", "Actually, hold off sending for now.", at(24, 12))
    edition = await store.open_edition()
    assert edition is not None and edition.state == "in_review"
    assert [e.subject for e in conversation.sent if e.subject.startswith("Send cancelled:")] == [
        "Send cancelled: Pulse: 22 September 2026"
    ]

    # 5. Second approval.
    await reviewer_says("m5", "Sorry, go ahead: approve v2.", at(25))
    edition = await store.open_edition()
    assert edition is not None and edition.state == "approved"

    # 6. The periodic check sends to all staff once, replying to the submissions mailbox.
    clock.now = at(25, 9, 1)
    result = await run_periodic_check(config, conversation, store, clock)
    assert result.status == "sent"

    [newsletter] = [e for e in conversation.sent if e.to == [config.all_staff]]
    assert newsletter.reply_to == [config.mailboxes.submissions]
    assert "tidied up" in newsletter.html
    assert "Review" not in newsletter.html
    assert [e.subject for e in conversation.sent if e.subject.startswith("Sent:")] == [
        "Sent: Pulse: 22 September 2026"
    ]
    final = await store.edition_for_build(build.edition.build_id)
    assert final is not None and final.state == "sent"
