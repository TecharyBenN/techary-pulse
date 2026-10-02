"""The review section of a reviewer email."""

from collections.abc import Sequence

from pulse.entities.base import Entity
from pulse.entities.content import CheckName, Content, NotApplied, Version, item_sources
from pulse.entities.extracts import Consolidation, ExtractRecord
from pulse.entities.mail import ScreenedEmail


class Exclusion(Entity):
    """An excluded record with its email."""

    email: ScreenedEmail
    record: ExtractRecord


class SourceLink(Entity):
    """An entry's text with the source emails of its item."""

    text: str
    sources: list[ScreenedEmail]


class Finding(Entity):
    """A check failure or unsupported claim, named as reviewers see the newsletter."""

    # The part of the newsletter it is in: an entry is named by its text, a section by its title.
    where: str
    problem: str


class Review(Entity):
    """The review section's lists, in the order the reviewer email shows them."""

    check_failures: list[Finding]
    # None when the version was not judged.
    unsupported: list[Finding] | None
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
    # Every record code excluded, including those since restored, which keep their excluded ID.
    excluded = [
        Exclusion(email=by_message[record.message_id], record=record)
        for record in records
        if record.excluded_id is not None
    ]
    content = version.content
    sources = item_sources(content, consolidation.items if consolidation else [], emails)
    # A first draft has no earlier version to change, so it shows no changes.
    revised = version.version > 1
    return Review(
        check_failures=[
            Finding(
                where=_where(content, failure.target),
                problem=f"{_CHECK_TITLES[failure.check]}: {failure.detail}",
            )
            for failure in version.check_failures
        ],
        unsupported=None
        if version.verdicts is None
        else [
            Finding(where=_where(content, verdict.target), problem=str(verdict.claim))
            for verdict in version.verdicts
            if not verdict.supported
        ],
        changes=version.changes if revised else [],
        not_applied=version.not_applied if revised else [],
        restored=[e for e in excluded if e.record.restored_by is not None],
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
            for entry in content.entries()
        ],
    )


_CHECK_TITLES: dict[CheckName, str] = {
    "word_count": "Word count",
    "dashes": "Dashes",
    "digits": "Number not in the sources or feedback",
    "people": "Name",
    "sentences": "Too many sentences",
    "senders": "Sender",
    "items": "Item",
    "categories": "Section",
}
_PART_NAMES = {"headline_title": "Headline title", "headline": "Headline", "intro": "Intro"}


def _where(content: Content, target: str | None) -> str:
    """Reviewers never see item IDs or categories, so a target is named by what they show."""
    if target is None:
        return "Whole newsletter"
    if target in _PART_NAMES:
        return _PART_NAMES[target]
    texts = {entry.item_id: entry.text for entry in content.entries()}
    titles = {section.category: section.title for section in content.sections}
    return texts.get(target) or titles.get(target) or target
