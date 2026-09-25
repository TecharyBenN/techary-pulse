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
    reply_to: list[str]
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
    """A consolidated item with the sender names code adds from its source messages."""

    sender_names: list[str]


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
