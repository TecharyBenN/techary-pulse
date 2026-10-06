from datetime import UTC, datetime
from typing import Any

import pytest

from pulse.entities.content import (
    CheckFailure,
    Claim,
    Content,
    Entry,
    EntryNotes,
    FlaggedEntry,
    Verdict,
    check_content,
    draft_changed,
    entry_credits,
    entry_flags,
    flagged_entries,
    item_sources,
    require_draft,
    source_text,
    version_of,
)
from pulse.entities.errors import Refusal
from pulse.entities.extracts import Sensitivity, restored
from pulse.entities.mail import ScreenedEmail, screen
from tests.emails import (
    make_draft,
    make_email,
    make_extract_record,
    make_item,
    make_screened_email,
)

SECTION_TITLES = {"customer_win": "Customer wins", "shout_out": "Shout-outs"}
NOTES = EntryNotes(credits={}, flags={})


def _source(sender_name: str, subject: str, body: str) -> ScreenedEmail:
    email = make_email(sender_name=sender_name, subject=subject, body=body)
    return screen(email, ["example.org"], [], [])


SOURCES = {
    "item-1": [
        _source(
            "Priya Shah",
            "Signed Northwind Retail",
            "Tom Evans and I signed Northwind Retail on 22 September.",
        )
    ],
    "item-2": [
        _source("Dan Wood", "Thank you Sam", "Huge thanks to Sam Patel for covering the desk.")
    ],
}


def _content(
    intro: str = "A new customer and some well-earned thanks.",
    entries: list[tuple[str, str, str, list[str]]] | None = None,
    item_ids: list[str] | None = None,
    headline: str = "A new retail customer",
) -> Content:
    if entries is None:
        entries = [
            (
                "customer_win",
                "item-1",
                "Priya Shah and Tom Evans signed Northwind Retail on 22 September.",
                ["Priya Shah", "Tom Evans"],
            ),
            ("shout_out", "item-2", "Dan Wood thanks Sam Patel.", ["Dan Wood", "Sam Patel"]),
        ]
    sections: dict[str, list[dict[str, Any]]] = {}
    for category, item_id, text, people in entries:
        sections.setdefault(category, []).append(
            {"item_id": item_id, "text": text, "people": people}
        )
    return Content.model_validate(
        {
            "headline_title": "Headline of the week",
            "headline": headline,
            "intro": intro,
            "sections": [
                {"category": c, "title": SECTION_TITLES.get(c, c), "entries": e}
                for c, e in sections.items()
            ],
            "item_ids": ["item-1", "item-2"] if item_ids is None else item_ids,
        }
    )


def _check(
    content: Content,
    feedback: list[str] | None = None,
    max_words: int = 400,
    sources: dict[str, list[ScreenedEmail]] | None = None,
) -> list[CheckFailure]:
    return check_content(
        content,
        SOURCES if sources is None else sources,
        feedback or [],
        list(SECTION_TITLES),
        max_words,
    )


def _checks(failures: list[CheckFailure]) -> list[tuple[str, str | None]]:
    return [(failure.check, failure.target) for failure in failures]


def test_valid_content_passes() -> None:
    assert _check(_content()) == []


# Headline of the week (4), headline (4), intro (7), two titles (2 + 1), entries (11 + 5).
WORDS = 34


def test_word_count_at_limit_passes() -> None:
    assert _check(_content(), max_words=WORDS) == []


def test_word_count_over_limit_fails() -> None:
    assert _checks(_check(_content(), max_words=WORDS - 1)) == [("word_count", None)]


def test_word_count_omits_titles_of_empty_sections() -> None:
    content = _content()
    empty = content.sections[0].model_copy(update={"entries": []})
    with_empty = content.model_copy(update={"sections": [*content.sections, empty]})

    assert _check(with_empty, max_words=WORDS) == []


@pytest.mark.parametrize(
    ("changes", "target"),
    [
        ({"intro": "A new customer \N{EM DASH} and thanks."}, "intro"),
        ({"intro": "Customers 2025\N{EN DASH}2026 and thanks."}, "intro"),
        ({"headline": "A new retail customer \N{EN DASH} at last"}, "headline"),
    ],
)
def test_dashes_fail(changes: dict[str, Any], target: str) -> None:
    assert ("dashes", target) in _checks(_check(_content(**changes)))


def test_dash_in_entry_fails() -> None:
    content = _content(
        entries=[
            (
                "customer_win",
                "item-1",
                "Priya Shah \N{EM DASH} with Tom Evans \N{EM DASH} signed.",
                [],
            ),
            ("shout_out", "item-2", "Dan Wood thanks Sam Patel.", []),
        ]
    )

    assert _checks(_check(content)) == [("dashes", "item-1")]


def test_hyphen_passes() -> None:
    assert _check(_content(intro="A well-earned thank-you.")) == []


def _entry_one(text: str, people: list[str] | None = None) -> Content:
    return _content(
        entries=[
            ("customer_win", "item-1", text, people or []),
            ("shout_out", "item-2", "Dan Wood thanks Sam Patel.", []),
        ]
    )


def test_digit_not_in_sources_fails() -> None:
    failures = _check(_entry_one("Priya Shah signed a 3 year deal."))

    assert _checks(failures) == [("digits", "item-1")]
    assert "3" in failures[0].detail


def test_digit_from_another_item_fails() -> None:
    sources = SOURCES | {"item-2": [_source("Dan Wood", "Thanks", "Sam Patel covered 3 shifts.")]}

    failures = _check(_entry_one("Priya Shah signed a 3 year deal."), sources=sources)

    assert _checks(failures) == [("digits", "item-1")]


def test_digit_inside_a_longer_source_number_passes() -> None:
    assert _check(_entry_one("Priya Shah signed on 22 Sept, the 2nd deal.")) == []


def test_digit_in_feedback_passes() -> None:
    feedback = ["Please say it is a 3 year deal"]

    assert _check(_entry_one("Priya Shah signed a 3 year deal."), feedback=feedback) == []


def test_numbers_as_words_are_not_checked() -> None:
    assert _check(_entry_one("Priya Shah signed a three year deal.")) == []


def test_intro_digit_from_any_item_passes() -> None:
    assert _check(_content(intro="Big news on 22 September.")) == []


def test_intro_digit_not_in_sources_fails() -> None:
    assert _checks(_check(_content(intro="Big news for 2027."))) == [("digits", "intro")]


def test_name_missing_from_entry_text_is_left_to_the_writer() -> None:
    # The writer's output checks ensure every name in people is in the text.
    assert _check(_entry_one("Priya Shah signed a deal.", ["Priya Shah", "Tom Evans"])) == []


def test_name_not_in_sources_fails() -> None:
    failures = _check(_entry_one("Priya Shah and Ann Lee signed.", ["Priya Shah", "Ann Lee"]))

    assert _checks(failures) == [("people", "item-1")]
    assert "Ann Lee" in failures[0].detail


def test_name_matches_ignoring_case() -> None:
    assert _check(_entry_one("PRIYA SHAH and tom evans signed.", ["priya shah", "Tom Evans"])) == []


def test_name_from_sender_names_passes() -> None:
    sources = SOURCES | {"item-1": [_source("Priya Shah", "Signed", "Signed today.")]}

    assert _check(_entry_one("Priya Shah signed.", ["Priya Shah"]), sources=sources) == []


def test_name_from_feedback_passes() -> None:
    content = _entry_one("Priya Shah and Ann Lee signed.", ["Priya Shah", "Ann Lee"])

    assert _check(content, feedback=["Ann Lee helped too"]) == []


@pytest.mark.parametrize(
    "text",
    [
        "Priya Shah signed. It went well",
        "Priya Shah signed! It went well?",
        "Priya Shah signed...and it went well. Onboarding follows.",
        "Priya Shah signed Northwind.com today. It went well.",
    ],
)
def test_two_sentences_pass(text: str) -> None:
    assert _check(_entry_one(text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "Priya Shah signed. It went well. Onboarding follows.",
        "Priya Shah signed. It went well! Onboarding follows",
        "Priya Shah signed, e.g. the deal. It went well.",
    ],
)
def test_three_sentences_fail(text: str) -> None:
    assert _checks(_check(_entry_one(text))) == [("sentences", "item-1")]


def test_an_entry_need_not_name_its_sender() -> None:
    # The credit line names who shared the news, so the entry tells the news itself.
    content = _content(
        entries=[
            ("customer_win", "item-1", "Northwind Retail signed on 22 September.", []),
            ("shout_out", "item-2", "Thanks to Sam Patel.", ["Sam Patel"]),
        ]
    )

    assert _check(content) == []


def test_entry_for_item_not_included_fails() -> None:
    content = _content(item_ids=["item-1"])

    assert _checks(_check(content)) == [("items", "item-2")]


def test_entry_for_unknown_item_fails() -> None:
    content = _content(
        entries=[
            ("customer_win", "item-1", "Priya Shah signed Northwind Retail.", []),
            ("shout_out", "item-2", "Dan Wood thanks Sam Patel.", []),
            ("shout_out", "item-9", "Something else.", []),
        ],
        item_ids=["item-1", "item-2", "item-9"],
    )

    assert _checks(_check(content)) == [("items", "item-9")]


def test_included_item_without_entry_fails() -> None:
    content = _content(item_ids=["item-1", "item-2", "item-3"])

    assert _checks(_check(content)) == [("items", "item-3")]


def test_included_item_appearing_twice_fails() -> None:
    content = _content(
        entries=[
            ("customer_win", "item-1", "Priya Shah signed Northwind Retail.", []),
            ("shout_out", "item-2", "Dan Wood thanks Sam Patel.", []),
            ("shout_out", "item-2", "Dan Wood thanks Sam Patel again.", []),
        ]
    )

    assert _checks(_check(content)) == [("items", "item-2")]


def test_unconfigured_category_fails() -> None:
    content = _content(
        entries=[
            ("customer_win", "item-1", "Priya Shah signed Northwind Retail.", []),
            ("office_news", "item-2", "Dan Wood thanks Sam Patel.", []),
        ]
    )

    assert _checks(_check(content)) == [("categories", "office_news")]


def test_every_failure_is_returned() -> None:
    content = _content(intro="Big news for 2027 \N{EM DASH} really.")

    assert _checks(_check(content, max_words=5)) == [
        ("word_count", None),
        ("dashes", "intro"),
        ("digits", "intro"),
    ]


def test_item_sources_maps_each_included_item_to_its_emails() -> None:
    emails = [make_screened_email(m) for m in ("m01", "m02", "m03")]
    content = make_draft(
        Entry(item_id="item-1", text="", people=[]),
        Entry(item_id="item-9", text="", people=[]),
    ).content

    sources = item_sources(content, [make_item(source_message_ids=["m01", "m02"])], emails)

    # An unknown ID is left out, so the items check reports it.
    assert sources == {"item-1": emails[:2]}


def test_draft_changed_compares_the_content_with_the_latest_version() -> None:
    draft = make_draft()
    latest = version_of(draft, 1, datetime(2026, 9, 26, 9, 0, tzinfo=UTC), [], None, NOTES)
    revised = make_draft(Entry(item_id="item-1", text="Shorter.", people=[]))

    assert draft_changed(None, None) is False
    assert draft_changed(draft, None) is True
    assert draft_changed(draft, latest) is False
    assert draft_changed(revised, latest) is True


def test_require_draft_refuses_when_there_is_no_working_draft() -> None:
    assert require_draft(make_draft()) == make_draft()
    with pytest.raises(Refusal, match="there is no working draft"):
        require_draft(None)


def test_source_text_is_subject_and_body() -> None:
    text = source_text(_source("Priya Shah", "Signed Northwind Retail", "On 22 September."))

    assert text == "Signed Northwind Retail\nOn 22 September."


def test_source_text_of_rejected_screened_email_is_subject_only() -> None:
    email = make_email(sender_address="alex.morgan@example.com", subject="Signed Northwind Retail")

    assert source_text(screen(email, ["example.org"], [], [])) == "Signed Northwind Retail"


def test_names_and_digits_in_a_forwarded_message_are_in_the_sources() -> None:
    email = make_email(
        sender_name="Ben Carter",
        subject="FW: Price changes",
        unique_body="Sharing this for everyone who orders laptops.",
        body="Sharing this for everyone who orders laptops.\n\n"
        "From: Jo King, Litware\nLaptop prices rise by 5 percent on 1 November.",
    )
    sources = {"item-1": [screen(email, ["example.org"], [], [])]}
    content = _content(
        entries=[
            (
                "customer_win",
                "item-1",
                "Ben Carter shares that Jo King at Litware is raising prices by 5 percent.",
                ["Ben Carter", "Jo King"],
            )
        ],
        item_ids=["item-1"],
        intro="Prices change on 1 November.",
    )

    assert _check(content, sources=sources) == []


COMMERCIAL = Sensitivity(kind="financial", withheld=False, evidence="a supplier's prices")
NAMED = Sensitivity(kind="named_person", withheld=False, evidence="thanks a colleague by name")
PRIVATE = Sensitivity(
    kind="personal_information", withheld=True, evidence="mentions a colleague's health"
)
FLAG_RECORDS = [
    make_extract_record("m01", sensitivity=[COMMERCIAL]),
    make_extract_record("m02"),
    make_extract_record("m03", sensitivity=[NAMED]),
    make_extract_record("m04"),
]
FLAG_ITEMS = [
    make_item("item-1", source_message_ids=["m01", "m02", "m03"]),
    make_item("item-2", source_message_ids=["m04"]),
]


def test_an_entry_carries_every_flag_of_its_items_source_records() -> None:
    flags = entry_flags(_content(), FLAG_ITEMS, FLAG_RECORDS)

    # The unflagged entry is left out.
    assert flags == {"item-1": [COMMERCIAL, NAMED]}


def test_a_restored_record_keeps_the_flags_it_was_excluded_for() -> None:
    record = restored(
        [make_extract_record("m04", sensitivity=[PRIVATE])], "excluded-1", "reviewer-oid"
    )

    flags = entry_flags(_content(), FLAG_ITEMS, [*FLAG_RECORDS[:3], record])

    assert flags["item-2"] == [PRIVATE]


def test_flags_come_from_the_records_not_the_entry() -> None:
    records = [make_extract_record(m) for m in ("m01", "m02", "m03")]

    flags = entry_flags(_content(), FLAG_ITEMS, [*records, FLAG_RECORDS[3]])

    assert flags == {}


def test_an_entry_naming_no_item_has_no_flags() -> None:
    content = _content(entries=[("customer_win", "item-9", "Unknown.", [])], item_ids=["item-9"])

    assert entry_flags(content, FLAG_ITEMS, FLAG_RECORDS) == {}


def test_flagged_entries_list_each_flagged_entry_in_newsletter_order() -> None:
    content = _content(
        entries=[
            ("customer_win", "item-2", "Unflagged.", []),
            ("customer_win", "item-1", "First.", []),
            ("shout_out", "item-1", "Second, sharing the item.", []),
        ]
    )
    flags = entry_flags(content, FLAG_ITEMS, FLAG_RECORDS)

    assert flagged_entries(content, flags) == [
        FlaggedEntry(text="First.", flags=[COMMERCIAL, NAMED]),
        FlaggedEntry(text="Second, sharing the item.", flags=[COMMERCIAL, NAMED]),
    ]


def test_each_included_item_is_credited_to_its_senders_once_each() -> None:
    emails = [
        make_screened_email("m01", sender_name="Priya Shah"),
        make_screened_email("m02", sender_name="Tom Evans"),
        make_screened_email("m03", sender_name="Priya Shah"),
    ]
    items = [make_item("item-1", source_message_ids=["m01", "m02", "m03"])]

    assert entry_credits(_content(item_ids=["item-1"]), items, emails) == {
        "item-1": ["Priya Shah", "Tom Evans"]
    }


def test_a_version_keeps_the_notes_it_was_presented_with() -> None:
    notes = EntryNotes(credits={"item-1": ["Priya Shah"]}, flags={"item-1": [COMMERCIAL]})

    version = version_of(make_draft(), 1, datetime(2026, 9, 26, 9, 0, tzinfo=UTC), [], None, notes)

    assert version.notes == notes


def test_a_verdict_is_supported_exactly_when_every_claim_has_a_source() -> None:
    sourced = Claim(claim="Signed Northwind Retail", source="Signed Northwind Retail today")
    unsourced = Claim(claim="Signed two customers", source=None)

    assert Verdict(target="item-1", claims=[sourced]).supported
    assert not Verdict(target="item-1", claims=[sourced, unsourced]).supported
    assert Verdict(target="item-1", claims=[sourced, unsourced]).unsupported() == [
        "Signed two customers"
    ]


def test_a_text_with_no_claims_is_supported() -> None:
    # Warm wording such as a greeting states no fact, so it makes no claim.
    verdict = Verdict(target="item-1", claims=[])

    assert verdict.supported and verdict.unsupported() == []
