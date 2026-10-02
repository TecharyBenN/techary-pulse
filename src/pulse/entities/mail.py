"""Email: its types, the Mailbox interface, the pre-filter and the email channel's header checks."""

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime

from pulse.entities.base import Entity

# Graph's immutable message ID, which stays the same when a message moves folders.
MessageId = str

RejectionReason = Literal[
    "sender_domain", "sender_not_allowed", "sensitivity_label", "automatic_reply"
]
# Why the email channel gives a message no orchestrator run.
ChannelRejection = Literal["not_internal", "automatic_reply"]
# The folder delivery moves a screened email to once its newsletter is sent.
Destination = Literal["processed", "rejected"]

# Label IDs are GUIDs, so the first underscore after the ID starts the property name.
_LABEL_ENTRY = re.compile(r"MSIP_Label_([^_=]+)_([^=]+)=(.*)")


class Email(Entity):
    """A message received in a Pulse mailbox."""

    message_id: MessageId
    sender_name: str
    sender_address: str
    subject: str
    received: AwareDatetime
    has_attachments: bool


class InboundEmail(Email):
    """A received message as listed from an inbox."""

    # Graph's uniqueBody: the new content only, without quoted replies.
    body: str
    headers: dict[str, str]


class Body(Entity):
    """An email body, in HTML or plain text."""

    content: str
    content_type: Literal["html", "text"]


class OutboundEmail(Entity):
    """A new email for a Mailbox to send."""

    to: list[str]
    subject: str
    body: Body
    reply_to: str | None


class ScreenedEmail(Email):
    """An email from the submissions mailbox after the pre-filter, as Pulse stores it."""

    rejection: RejectionReason | None
    # Kept only when the email passed the pre-filter, so rejected content is never stored.
    body: str | None
    # Whether delivery has moved it out of the inbox.
    moved: bool


class Mailbox(Protocol):
    """A Pulse mailbox: the submissions mailbox or the conversation mailbox."""

    async def list_inbox(self) -> list[InboundEmail]:
        """Every inbox message, sorted by received time, then message ID."""
        ...

    async def move(self, message_id: MessageId, folder: str) -> None:
        """Move a message to the named folder, creating the folder if it is absent."""
        ...

    async def send(self, email: OutboundEmail) -> MessageId:
        """Send a new message; return its ID."""
        ...

    async def reply(self, message_id: MessageId, to: Sequence[str], body: Body) -> MessageId:
        """Reply in the message's thread to `to` only, keeping the thread's subject; return the
        reply's ID."""
        ...


def address_in(address: str, addresses: Iterable[str]) -> bool:
    """Compare email addresses exactly, ignoring case."""
    return address.casefold() in {candidate.casefold() for candidate in addresses}


def domain_in(address: str, domains: Iterable[str]) -> bool:
    _, at, domain = address.rpartition("@")
    return bool(at) and domain.casefold() in {candidate.casefold() for candidate in domains}


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Header names are case-insensitive (RFC 5322)."""
    wanted = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == wanted), None)


def is_automatic_reply(headers: Mapping[str, str]) -> bool:
    auto_submitted = header_value(headers, "Auto-Submitted")
    return (
        auto_submitted is not None and auto_submitted.strip().casefold() != "no"
    ) or header_value(headers, "X-Auto-Response-Suppress") is not None


def channel_rejection(headers: Mapping[str, str]) -> ChannelRejection | None:
    """The email channel's header checks, in order; the sender is resolved separately."""
    # Exchange adds this header to mail it authenticated as from inside the organisation.
    auth_as = header_value(headers, "X-MS-Exchange-Organization-AuthAs")
    if auth_as is None or auth_as.strip().casefold() != "internal":
        return "not_internal"
    if is_automatic_reply(headers):
        return "automatic_reply"
    return None


def screen(
    email: InboundEmail,
    allowed_sender_domains: Sequence[str],
    allowed_senders: Sequence[str],
    allowed_sensitivity_labels: Sequence[str],
) -> ScreenedEmail:
    """Apply the pre-filter to an inbox message."""
    rejection = _rejection(
        email, allowed_sender_domains, allowed_senders, allowed_sensitivity_labels
    )
    return ScreenedEmail(
        **email.model_dump(include=set(Email.model_fields)),
        rejection=rejection,
        body=email.body if rejection is None else None,
        moved=False,
    )


def destination(email: ScreenedEmail, extracted: bool) -> Destination | None:
    """Where a sent newsletter's screened email goes; one not extracted stays pending."""
    if email.rejection is not None:
        return "rejected"
    return "processed" if extracted else None


def display_date(moment: datetime, timezone: ZoneInfo) -> str:
    """The date in `timezone`, as in 25 September 2026."""
    local = moment.astimezone(timezone)
    return f"{local.day} {local:%B %Y}"


def subject(template: str, opened_at: datetime, timezone: ZoneInfo, prefix: str = "") -> str:
    """`subject_template` with `{date}` as the date the newsletter was opened, after `prefix`."""
    text = template.replace("{date}", display_date(opened_at, timezone))
    return f"{prefix} {text}" if prefix else text


def _rejection(
    email: InboundEmail,
    allowed_sender_domains: Sequence[str],
    allowed_senders: Sequence[str],
    allowed_sensitivity_labels: Sequence[str],
) -> RejectionReason | None:
    if not domain_in(email.sender_address, allowed_sender_domains):
        return "sender_domain"
    if allowed_senders and not address_in(email.sender_address, allowed_senders):
        return "sender_not_allowed"
    if not _labels_allowed(email.headers, allowed_sensitivity_labels):
        return "sensitivity_label"
    if is_automatic_reply(email.headers):
        return "automatic_reply"
    return None


def _labels_allowed(headers: Mapping[str, str], allowed: Sequence[str]) -> bool:
    value = header_value(headers, "msip_labels")
    if value is None:
        return True
    allowed_ids = {label.casefold() for label in allowed}
    for entry in filter(None, (part.strip() for part in value.split(";"))):
        match = _LABEL_ENTRY.fullmatch(entry)
        # A header Pulse cannot read may hide a label, so it is not allowed.
        if match is None:
            return False
        label_id, name, setting = match.groups()
        enabled = name == "Enabled" and setting.strip().casefold() == "true"
        if enabled and label_id.casefold() not in allowed_ids:
            return False
    return True
