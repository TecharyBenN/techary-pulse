"""Submissions and the pre-filter."""

import re
from collections.abc import Mapping, Sequence
from typing import Literal

from pulse.entities.mail import (
    Email,
    InboundEmail,
    address_in,
    domain_in,
    header_value,
    is_automatic_reply,
)

RejectionReason = Literal[
    "sender_domain", "sender_not_allowed", "sensitivity_label", "automatic_reply"
]

# Label IDs are GUIDs, so the first underscore after the ID starts the property name.
_LABEL_ENTRY = re.compile(r"MSIP_Label_([^_=]+)_([^=]+)=(.*)")


class Submission(Email):
    rejection: RejectionReason | None
    # Kept only for submissions that passed the pre-filter.
    body: str | None


def screen(
    email: InboundEmail,
    allowed_sender_domains: Sequence[str],
    allowed_senders: Sequence[str],
    allowed_sensitivity_labels: Sequence[str],
) -> Submission:
    """Apply the pre-filter to an inbox message and return it as a submission."""
    rejection = _rejection(
        email, allowed_sender_domains, allowed_senders, allowed_sensitivity_labels
    )
    return Submission(
        **email.model_dump(include=set(Email.model_fields)),
        rejection=rejection,
        body=email.body if rejection is None else None,
    )


def source_text(submission: Submission) -> str:
    """The text names and numbers in a draft are checked against."""
    return "\n".join(part for part in (submission.subject, submission.body) if part)


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
