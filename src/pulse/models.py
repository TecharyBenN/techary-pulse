"""Messages, extract records, items, drafts and judge results.

Agent output types use only basic types, lists of allowed values, required fields and no
extra fields, so native structured output works whichever provider sits behind the gateway.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(_Frozen):
    """A message in the Pulse mailbox inbox."""

    id: str
    sender_name: str
    sender_address: str
    subject: str
    received_at: datetime
    body: str
    headers: dict[str, str]
    has_attachments: bool


class CleanedEmail(_Frozen):
    """What the extractor receives for one message."""

    message_id: str
    sender_name: str
    sender_address: str
    subject: str
    received_at: datetime
    body: str

    @classmethod
    def from_message(cls, message: Message) -> CleanedEmail:
        return cls(
            message_id=message.id,
            sender_name=message.sender_name,
            sender_address=message.sender_address,
            subject=message.subject,
            received_at=message.received_at,
            body=message.body,
        )


class OutgoingEmail(_Frozen):
    to: list[str]
    reply_to: list[str] = []
    subject: str
    html: str


class Sensitivity(_Output):
    type: Literal["commercial", "personal", "unannounced", "inappropriate"]
    evidence: str


class ExtractRecord(_Output):
    message_id: str
    is_update: bool
    exclusion_reason: Literal["not_an_update", "unclear", "no_matching_section"] | None
    category: str | None
    summary: str
    facts: list[str]
    people: list[str]
    sensitivity: list[Sensitivity]


class Item(_Output):
    item_id: str
    category: str
    facts: list[str]
    people: list[str]
    source_message_ids: list[str]


class ItemWithSenders(Item):
    """A consolidated item with the sender names and received dates code adds from its sources."""

    sender_names: list[str]
    received_dates: list[datetime]


class Consolidation(_Output):
    headline: str
    items: list[Item]


class DraftEntry(_Output):
    item_id: str
    text: str
    people: list[str]


class DraftSection(_Output):
    category: str
    entries: list[DraftEntry]


class Draft(_Output):
    intro: str
    sections: list[DraftSection]


class Verdict(_Output):
    supported: bool
    reason: str


class EntryVerdict(Verdict):
    item_id: str


class JudgeResult(_Output):
    intro: Verdict
    entries: list[EntryVerdict]


class DraftInput(_Frozen):
    items: list[ItemWithSenders]
    max_words: int
    failures: list[str]


class JudgeInput(_Frozen):
    draft: Draft
    items: list[ItemWithSenders]
    feedback: list[str] = []


class NotApplied(BaseModel):
    """Feedback the reviser could not apply, with the reason; stored unchanged in the version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feedback: str
    reason: str


class ExcludedForReviser(_Frozen):
    """An excluded record as given to the reviser, so it can restore one by item ID."""

    item_id: str
    record: ExtractRecord
    reason: str
    sender_names: list[str]


class ReviseInput(_Frozen):
    draft: Draft
    headline: str
    items: list[ItemWithSenders]
    excluded: list[ExcludedForReviser]
    feedback: list[str]
    instruction: str
    reviewer_message: str
    failures: list[str]


class Revision(_Output):
    headline: str
    draft: Draft
    item_ids: list[str]
    changes: list[str]
    not_applied: list[NotApplied]


EditionState = Literal["in_review", "approved", "sent", "expired", "discarded"]
Trigger = Literal["schedule", "reviewer", "command"]
Channel = Literal["email", "librechat"]


class Edition(_Frozen):
    """An edition: one newsletter from its build until it is sent, expired or discarded.

    The ID is also the ``conversation_id`` of the edition's conversation history.
    """

    id: str
    build_id: str
    state: EditionState
    trigger: Trigger
    created_at: datetime
    current_version: int
    approved_version: int | None = None
    approver: str | None = None
    approved_at: datetime | None = None
    send_at: datetime | None = None
    send_started: bool = False
    sent_at: datetime | None = None
    closed_at: datetime | None = None


class Version(_Frozen):
    """One draft version of an edition."""

    number: int
    draft: Draft
    headline: str
    item_ids: list[str]
    check_results: list[str]
    changes: list[str] = []
    not_applied: list[NotApplied] = []
    creator: str
    created_at: datetime


class Feedback(_Frozen):
    reviewer: str
    channel: Channel
    text: str
    received_at: datetime


class ReviewerMessage(Feedback):
    """A reviewer message from either channel: the feedback it is saved as, and who sent it."""

    reviewer_name: str
