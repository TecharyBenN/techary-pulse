"""The review section of a reviewer email: what reviewers need to judge a version."""

from collections.abc import Sequence

from pulse.entities.base import Entity
from pulse.entities.content import NotApplied, Version, item_sources
from pulse.entities.extracts import Consolidation, ExtractRecord
from pulse.entities.mail import ScreenedEmail


class Exclusion(Entity):
    email: ScreenedEmail
    record: ExtractRecord


class SourceLink(Entity):
    """An entry's text with the source emails of its item."""

    text: str
    sources: list[ScreenedEmail]


class Review(Entity):
    """The review section's lists, in the order the reviewer email shows them."""

    changes: list[str]
    not_applied: list[NotApplied]
    restored: list[Exclusion]
    sensitivity_exclusions: list[Exclusion]
    other_exclusions: list[Exclusion]
    not_extracted: list[ScreenedEmail]
    rejected_subjects: list[str]
    with_attachments: list[ScreenedEmail]
    source_map: list[SourceLink]


def build_review(
    version: Version,
    consolidation: Consolidation | None,
    records: Sequence[ExtractRecord],
    emails: Sequence[ScreenedEmail],
) -> Review:
    by_message = {email.message_id: email for email in emails}
    extracted = {record.message_id for record in records}
    excluded = [
        Exclusion(email=by_message[record.message_id], record=record)
        for record in records
        if record.exclusion is not None
    ]
    content = version.content
    sources = item_sources(content, consolidation.items if consolidation else [], records, emails)
    # A first draft has no earlier version to change, so it shows no changes.
    revised = version.version > 1
    return Review(
        changes=version.changes if revised else [],
        not_applied=version.not_applied if revised else [],
        restored=[e for e in excluded if e.record.excluded_id in content.item_ids],
        sensitivity_exclusions=[e for e in excluded if e.record.exclusion == "sensitivity"],
        other_exclusions=[e for e in excluded if e.record.exclusion == "no_category"],
        not_extracted=[
            email
            for email in emails
            if email.rejection is None and email.message_id not in extracted
        ],
        rejected_subjects=[email.subject for email in emails if email.rejection is not None],
        with_attachments=[
            email for email in emails if email.has_attachments and email.message_id in extracted
        ],
        source_map=[
            SourceLink(text=entry.text, sources=sources.get(entry.item_id, []))
            for section in content.sections
            for entry in section.entries
        ],
    )
