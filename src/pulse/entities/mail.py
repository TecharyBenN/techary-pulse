"""Email as Pulse sees it, the mailbox interface, and the rules that apply to any email."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from pydantic import AwareDatetime

from pulse.entities.base import Entity


class Email(Entity):
    """A message received in a Pulse mailbox."""

    message_id: str
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


class OutboundEmail(Entity):
    to: list[str]
    subject: str
    html: str
    reply_to: str | None


class Mailbox(Protocol):
    """A Pulse mailbox; main creates one for the submissions mailbox and one for the
    conversation mailbox."""

    async def list_inbox(self) -> list[InboundEmail]:
        """Every inbox message, sorted by received time, then message ID."""
        ...

    async def move(self, message_id: str, folder: str) -> None:
        """Move a message to the named folder, creating the folder if it is absent."""
        ...

    async def send(self, email: OutboundEmail) -> None: ...

    async def reply(self, message_id: str, to: Sequence[str], text: str) -> None:
        """Reply in the message's thread to `to` only, with a plain-text body."""
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
