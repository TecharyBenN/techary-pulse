"""Deterministic rules: the pre-filter (step 3), exclusions after extraction, and section order."""

import re
from dataclasses import dataclass

from pulse.config import Config, SectionConfig, domain_of
from pulse.models import Draft, DraftEntry, ExtractRecord, Message

# msip_labels holds entries such as "MSIP_Label_<id>_Enabled=true"; one per applied label.
_ENABLED_LABEL = re.compile(r"MSIP_Label_([0-9a-fA-F-]+)_Enabled=true", re.IGNORECASE)


@dataclass(frozen=True)
class Rejection:
    message: Message
    reason: str


def label_ids(message: Message) -> set[str]:
    return {m.lower() for m in _ENABLED_LABEL.findall(message.headers.get("msip_labels", ""))}


def rejection_reason(message: Message, config: Config) -> str | None:
    """Return why the pre-filter rejects the message, or None if it passes."""
    sender = message.sender_address.strip().lower()
    try:
        domain = domain_of(sender)
    except ValueError:
        return "sender address is not valid"
    if domain not in {d.strip().lower() for d in config.allowed_sender_domains}:
        return "sender domain is not allowed"
    allowed_senders = {s.strip().lower() for s in config.allowed_senders}
    if allowed_senders and sender not in allowed_senders:
        return "sender is not allowed"
    allowed_labels = {label.strip().lower() for label in config.allowed_sensitivity_labels}
    if label_ids(message) - allowed_labels:
        return "sensitivity label is not allowed"
    auto_submitted = message.headers.get("auto-submitted")
    if auto_submitted is not None and auto_submitted.strip().lower() != "no":
        return "automatic reply"
    if "x-auto-response-suppress" in message.headers:
        return "automatic reply"
    if len(message.body.strip()) < config.limits.min_body_chars:
        return "body is too short"
    return None


def prefilter(messages: list[Message], config: Config) -> tuple[list[Message], list[Rejection]]:
    """Split messages into those that pass and those rejected, with reasons."""
    passed, rejected = [], []
    for message in messages:
        reason = rejection_reason(message, config)
        if reason is None:
            passed.append(message)
        else:
            rejected.append(Rejection(message, reason))
    return passed, rejected


@dataclass(frozen=True)
class Exclusion:
    record: ExtractRecord
    reason: str


def exclusion_reason(record: ExtractRecord) -> str | None:
    """Return why a record is excluded from the draft, or None if it is included."""
    if record.sensitivity:
        return "sensitivity"
    if record.exclusion_reason is not None:
        return record.exclusion_reason
    if not record.is_update:
        return "not_an_update"
    if record.category is None:
        return "no_matching_section"
    return None


def exclude(records: list[ExtractRecord]) -> tuple[list[ExtractRecord], list[Exclusion]]:
    included, excluded = [], []
    for record in records:
        reason = exclusion_reason(record)
        if reason is None:
            included.append(record)
        else:
            excluded.append(Exclusion(record, reason))
    return included, excluded


def sections_in_order(draft: Draft, config: Config) -> list[tuple[SectionConfig, list[DraftEntry]]]:
    """Draft sections in config order, omitting sections with no entries."""
    entries = {section.category: section.entries for section in draft.sections}
    return [(s, entries[s.category]) for s in config.sections if entries.get(s.category)]
