from datetime import UTC, datetime

from pulse.entities.content import (
    CheckFailure,
    Claim,
    Entry,
    NotApplied,
    Verdict,
    Version,
    version_of,
)
from pulse.entities.extracts import Sensitivity, make_record, restored
from pulse.entities.review import build_review
from tests.emails import (
    ENTRY,
    NO_NOTES,
    make_consolidation,
    make_draft,
    make_email,
    make_item,
    make_output,
    make_verdict,
    screen_email,
)

EMAILS = [
    screen_email(make_email("m01", has_attachments=True)),
    screen_email(make_email("m02", subject="Pricing update")),
    screen_email(make_email("m03", subject="Out of office")),
    screen_email(make_email("m04", subject="Not yet read")),
    screen_email(make_email("m05", subject="From outside", sender_address="a@example.com")),
    screen_email(make_email("m06", subject="Price agreed")),
]
SENSITIVE = Sensitivity(
    kind="confidential", withheld=True, evidence="mentions an unannounced price"
)
RECORDS = [
    make_record("m01", make_output(), []),
    make_record("m02", make_output(sensitivity=[SENSITIVE]), []),
    make_record("m03", make_output(category=None), ["excluded-1"]),
    restored(
        [make_record("m06", make_output(sensitivity=[SENSITIVE]), ["excluded-2"])],
        "excluded-3",
        "reviewer-oid",
    ),
]
CONSOLIDATION = make_consolidation(
    make_item(source_message_ids=["m01"]), make_item("item-2", source_message_ids=["m06"])
)


def _version(
    number: int,
    *entries: Entry,
    failures: list[CheckFailure] | None = None,
    verdicts: list[Verdict] | None = None,
) -> Version:
    draft = make_draft(
        *entries,
        changes=["Shortened the intro"],
        not_applied=[NotApplied(feedback="Add the price", reason="The price is not announced yet")],
    )
    created = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
    return version_of(draft, number, created, failures or [], verdicts, NO_NOTES)


def test_review_lists_every_group_for_its_version() -> None:
    restored = Entry(item_id="item-2", text="The price is agreed.", people=[])
    review = build_review(
        _version(2, Entry(item_id="item-1", text=ENTRY, people=[]), restored),
        CONSOLIDATION,
        RECORDS,
        EMAILS,
    )

    assert review.changes == ["Shortened the intro"]
    assert [n.feedback for n in review.not_applied] == ["Add the price"]
    # A restored record is included, so it is listed as restored and not as excluded.
    assert [r.record.excluded_id for r in review.restored] == ["excluded-3"]
    assert [e.email.subject for e in review.sensitivity_exclusions] == ["Pricing update"]
    assert [e.email.subject for e in review.other_exclusions] == ["Out of office"]
    assert [e.message_id for e in review.not_extracted] == ["m04"]
    assert review.rejected_subjects == ["From outside"]
    assert [e.message_id for e in review.with_attachments] == ["m01"]
    assert [(link.text, [e.message_id for e in link.sources]) for link in review.source_map] == [
        (ENTRY, ["m01"]),
        ("The price is agreed.", ["m06"]),
    ]


def test_first_version_shows_no_changes() -> None:
    review = build_review(_version(1), CONSOLIDATION, RECORDS, EMAILS)

    assert (review.changes, review.not_applied) == ([], [])


def test_failures_and_unsupported_claims_are_named_as_reviewers_see_them() -> None:
    failures = [
        CheckFailure(check="word_count", target=None, detail="420 words, limit 400"),
        CheckFailure(check="dashes", target="headline", detail="em or en dash"),
        CheckFailure(check="digits", target="item-1", detail="12"),
        CheckFailure(check="categories", target="customer_win", detail="not a configured category"),
    ]
    verdicts = [make_verdict("intro", claim="A record week"), make_verdict()]

    review = build_review(
        _version(1, failures=failures, verdicts=verdicts), CONSOLIDATION, RECORDS, EMAILS
    )

    assert [(f.where, f.problem) for f in review.check_failures] == [
        ("Whole newsletter", "Word count: 420 words, limit 400"),
        ("Headline", "Dashes: em or en dash"),
        (ENTRY, "Number not in the sources or feedback: 12"),
        ("Customer wins", "Section: not a configured category"),
    ]
    assert review.unsupported is not None
    assert [(f.where, f.problem) for f in review.unsupported] == [("Intro", "A record week")]


def test_each_unsupported_claim_is_its_own_finding() -> None:
    verdict = Verdict(
        target="item-1",
        claims=[
            Claim(claim="Signed two customers", source=None),
            Claim(claim="on 22 September", source="Signed Northwind Retail on 22 September"),
            Claim(claim="worth a million pounds", source=None),
        ],
    )

    review = build_review(_version(1, verdicts=[verdict]), CONSOLIDATION, RECORDS, EMAILS)

    assert review.unsupported is not None
    assert [(f.where, f.problem) for f in review.unsupported] == [
        (ENTRY, "Signed two customers"),
        (ENTRY, "worth a million pounds"),
    ]


def test_a_version_not_judged_has_no_unsupported_list() -> None:
    review = build_review(_version(1, verdicts=None), CONSOLIDATION, RECORDS, EMAILS)

    assert review.unsupported is None
