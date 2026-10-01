"""Operations, delivery and the renderer, configured as main configures them, for tests."""

import asyncio
from zoneinfo import ZoneInfo

from pulse.adapters.render import Renderer
from pulse.agents.orchestrator.run import history_note
from pulse.entities.clock import Clock
from pulse.entities.lifecycle import OnApproval, Scheduled
from pulse.entities.store import Store
from pulse.services.delivery import Delivery
from pulse.services.mail import Outbox
from pulse.services.operations import Operations
from tests.emails import screen_email
from tests.fakes.clock import ControlledClock
from tests.fakes.mailbox import FakeMailbox
from tests.messages import OPENED

REVIEWERS = "pulse-reviewers@example.org"
OPERATORS = ["pulse-operator@example.org"]
ALL_STAFF = "all-staff@example.org"
SUBMISSIONS = "pulse-submissions@example.org"
CATEGORIES = ("customer_win", "shout_out")
MAX_WORDS = 400
SUBJECT_TEMPLATE = "Pulse: {date}"
TIMEZONE = ZoneInfo("Europe/London")
ON_APPROVAL = OnApproval(mode="on_approval")


def make_renderer() -> Renderer:
    return Renderer(TIMEZONE)


def make_outbox(conversation: FakeMailbox, store: Store) -> Outbox:
    return Outbox(
        conversation,
        store,
        make_renderer().notice,
        reviewers=REVIEWERS,
        operator_alerts=OPERATORS,
        subject_template=SUBJECT_TEMPLATE,
        timezone=TIMEZONE,
    )


def make_operations(
    store: Store,
    submissions: FakeMailbox | None = None,
    conversation: FakeMailbox | None = None,
    clock: Clock | None = None,
    send_rule: OnApproval | Scheduled = ON_APPROVAL,
) -> Operations:
    return Operations(
        store,
        submissions or FakeMailbox(),
        make_outbox(conversation or FakeMailbox(), store),
        screen_email,
        make_renderer().reviewer_email,
        clock or ControlledClock(OPENED),
        send_rule=send_rule,
        timezone=TIMEZONE,
        categories=CATEGORIES,
        max_words=MAX_WORDS,
    )


def make_delivery(
    store: Store,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    clock: Clock,
    lock: asyncio.Lock,
) -> Delivery:
    return Delivery(
        store,
        conversation,
        submissions,
        make_outbox(conversation, store),
        make_renderer().newsletter,
        history_note,
        clock,
        lock,
        all_staff=ALL_STAFF,
        reply_to=SUBMISSIONS,
        subject_template=SUBJECT_TEMPLATE,
        timezone=TIMEZONE,
        processed_folder="Processed",
        rejected_folder="Rejected",
    )
