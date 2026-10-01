from datetime import UTC, datetime

from pulse.entities.content import Entry, NotApplied, Version, version_of
from pulse.entities.extracts import Sensitivity, make_record
from pulse.entities.review import build_review
from tests.emails import (
    ENTRY,
    make_consolidation,
    make_draft,
    make_email,
    make_item,
    make_output,
    screen_email,
)

EMAILS = [
    screen_email(make_email("m01", has_attachments=True)),
    screen_email(make_email("m02", subject="Pricing update")),
    screen_email(make_email("m03", subject="Out of office")),
    screen_email(make_email("m04", subject="Not yet read")),
    screen_email(make_email("m05", subject="From outside", sender_address="a@example.com")),
]
SENSITIVE = Sensitivity(type="commercial", evidence="mentions a price")
RECORDS = [
    make_record("m01", make_output(), None, []),
    make_record("m02", make_output(sensitivity=[SENSITIVE]), None, []),
    make_record("m03", make_output(category=None), None, ["excluded-1"]),
]
CONSOLIDATION = make_consolidation(make_item(source_message_ids=["m01"]))


def _version(number: int, *entries: Entry) -> Version:
    draft = make_draft(
        *entries,
        changes=["Shortened the intro"],
        not_applied=[NotApplied(feedback="Add the price", reason="Commercially sensitive")],
    )
    return version_of(draft, number, datetime(2026, 9, 26, 9, 0, tzinfo=UTC))


def test_review_lists_every_group_for_its_version() -> None:
    restored = Entry(item_id="excluded-2", text="Out of office.", people=[])
    review = build_review(
        _version(2, Entry(item_id="item-1", text=ENTRY, people=[]), restored),
        CONSOLIDATION,
        RECORDS,
        EMAILS,
    )

    assert review.changes == ["Shortened the intro"]
    assert [n.feedback for n in review.not_applied] == ["Add the price"]
    assert [r.record.excluded_id for r in review.restored] == ["excluded-2"]
    assert [e.email.subject for e in review.sensitivity_exclusions] == ["Pricing update"]
    assert [e.email.subject for e in review.other_exclusions] == ["Out of office"]
    assert [e.message_id for e in review.not_extracted] == ["m04"]
    assert review.rejected_subjects == ["From outside"]
    assert [e.message_id for e in review.with_attachments] == ["m01"]
    assert [(link.text, [e.message_id for e in link.sources]) for link in review.source_map] == [
        (ENTRY, ["m01"]),
        ("Out of office.", ["m03"]),
    ]


def test_first_version_shows_no_changes() -> None:
    review = build_review(_version(1), CONSOLIDATION, RECORDS, EMAILS)

    assert (review.changes, review.not_applied, review.restored) == ([], [], [])
