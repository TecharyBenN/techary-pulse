from datetime import UTC, datetime
from typing import Any

import pytest

from pulse.entities.errors import Refusal
from pulse.entities.extracts import (
    Consolidation,
    ExtractedNews,
    Extraction,
    ExtractorOutput,
    ExtractRecord,
    Item,
    Sensitivity,
    SourcedItem,
    added_numbers,
    consolidation_input,
    exclusion_outcome,
    extracted_news,
    items_up_to_date,
    make_consolidation,
    make_record,
    number_problems,
    restored,
    sender_names,
    with_sources,
)
from tests.emails import (
    HEADLINE,
    make_consolidator_item,
    make_consolidator_output,
    make_extract_record,
    make_item,
    make_output,
    make_screened_email,
    share,
)
from tests.operations import SECTIONS

FLAG = Sensitivity(
    kind="personal_information", withheld=True, evidence="mentions a colleague's health"
)
COMMERCIAL = Sensitivity(kind="financial", withheld=False, evidence="a supplier's prices")
NAMED = Sensitivity(kind="named_person", withheld=False, evidence="thanks a colleague by name")
REVIEWER = "reviewer-oid"


def test_included_record_has_no_outcome() -> None:
    assert exclusion_outcome(make_output()) is None


@pytest.mark.parametrize(
    "kind", ["named_person", "personal_information", "financial", "confidential", "inappropriate"]
)
@pytest.mark.parametrize(("withheld", "expected"), [(True, "sensitivity"), (False, None)])
def test_only_a_withheld_flag_excludes_whatever_its_kind(
    kind: str, withheld: bool, expected: str | None
) -> None:
    flag = Sensitivity.model_validate({"kind": kind, "withheld": withheld, "evidence": "x"})

    assert exclusion_outcome(make_output(sensitivity=[flag])) == expected


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"sensitivity": [COMMERCIAL, FLAG]}, "sensitivity"),
        ({"sensitivity": [FLAG], "category": None}, "sensitivity"),
        ({"category": None}, "no_category"),
        ({"sensitivity": [COMMERCIAL], "category": None}, "no_category"),
        ({"sensitivity": [COMMERCIAL, NAMED]}, None),
    ],
)
def test_exclusion(changes: dict[str, Any], expected: str | None) -> None:
    assert exclusion_outcome(make_output(**changes)) == expected


def test_output_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        make_output(confidence=0.9)


@pytest.mark.parametrize("kind", ["secret", "commercial", "personal_named"])
def test_record_rejects_unknown_sensitivity_kind(kind: str) -> None:
    with pytest.raises(ValueError):
        Sensitivity.model_validate({"kind": kind, "withheld": False, "evidence": "x"})


def test_included_record_keeps_its_flags() -> None:
    record = _record(make_output(sensitivity=[COMMERCIAL, NAMED]))

    assert (record.exclusion, record.excluded_id) == (None, None)
    assert record.sensitivity == [COMMERCIAL, NAMED]


def _record(output: Extraction, *used: str) -> ExtractRecord:
    return make_record("m01", output, used)


def test_record_keeps_the_extraction_and_takes_the_message_id_from_code() -> None:
    output = make_output()

    record = _record(output)

    assert record.message_id == "m01"
    assert share(record, Extraction) == output


def test_first_excluded_record_is_numbered_from_one() -> None:
    record = _record(make_output(category=None))

    assert (record.exclusion, record.excluded_id) == ("no_category", "excluded-1")


def test_next_excluded_record_follows_the_highest_number() -> None:
    record = _record(make_output(category=None), "excluded-1", "excluded-3")

    assert record.excluded_id == "excluded-4"


def test_restoring_includes_the_record_in_the_section_named_and_records_the_reviewer() -> None:
    excluded = _record(make_output(category=None))

    record = restored(
        [_record(make_output()), excluded],
        "excluded-1",
        REVIEWER,
        section="shout-outs",
        sections=SECTIONS,
    )

    assert record == excluded.model_copy(
        update={"exclusion": None, "restored_by": REVIEWER, "category": "shout_out"}
    )


def test_restoring_a_record_with_a_category_keeps_it() -> None:
    withheld = make_output(sensitivity=[Sensitivity(kind="financial", withheld=True, evidence="e")])
    excluded = _record(withheld)

    record = restored([excluded], "excluded-1", REVIEWER, section="Shout-outs", sections=SECTIONS)

    assert record == excluded.model_copy(update={"exclusion": None, "restored_by": REVIEWER})


@pytest.mark.parametrize(
    ("output", "section", "reason"),
    [
        (
            make_output(category=None),
            None,
            "excluded-1 fits no section, so the reviewer must name one of: Customer wins, "
            "Shout-outs",
        ),
        (
            make_output(category=None),
            "Gossip",
            "Gossip is not a section; name one of: Customer wins, Shout-outs",
        ),
        (
            make_output(category=None, facts=[]),
            "Shout-outs",
            "excluded-1 has no facts to write from; the reviewer can give the news in their "
            "feedback instead",
        ),
    ],
)
def test_restoring_refuses_a_record_it_cannot_place(
    output: Extraction, section: str | None, reason: str
) -> None:
    with pytest.raises(Refusal) as refused:
        restored([_record(output)], "excluded-1", REVIEWER, section=section, sections=SECTIONS)

    assert str(refused.value) == reason


def test_restoring_refuses_without_a_reviewer() -> None:
    with pytest.raises(Refusal, match="no reviewer is present in this run"):
        restored(
            [_record(make_output(category=None))],
            "excluded-1",
            None,
            section="Shout-outs",
            sections=SECTIONS,
        )


def test_restoring_refuses_an_id_that_names_no_excluded_record() -> None:
    record = restored(
        [_record(make_output(category=None))],
        "excluded-1",
        REVIEWER,
        section="Shout-outs",
        sections=SECTIONS,
    )

    for records, excluded_id in (([record], "excluded-1"), ([], "excluded-2")):
        with pytest.raises(Refusal) as refused:
            restored(records, excluded_id, REVIEWER, section="Shout-outs", sections=SECTIONS)
        assert str(refused.value) == (
            f"{excluded_id} names no excluded record in the open newsletter"
        )


INCLUDED = make_extract_record("m01")
SECOND = make_extract_record("m02", people=["Tom Evans", "Aisha Khan"])
EXCLUDED = make_extract_record("m03", category=None)


def test_input_is_every_included_record() -> None:
    assert consolidation_input([INCLUDED, SECOND, EXCLUDED]) == [INCLUDED, SECOND]


def test_input_refuses_when_no_record_is_included() -> None:
    with pytest.raises(Refusal, match="the newsletter has no included extract records"):
        consolidation_input([EXCLUDED])


def test_consolidation_numbers_items_in_order_and_gives_them_their_sources_people() -> None:
    merged = make_consolidator_item("record-2", "record-1")
    single = make_consolidator_item("record-1", category="shout_out")

    consolidation = make_consolidation(make_consolidator_output(merged, single), [INCLUDED, SECOND])

    assert consolidation == Consolidation(
        headline=HEADLINE,
        items=[
            Item(
                category=merged.category,
                facts=merged.facts,
                source_message_ids=["m02", "m01"],
                item_id="item-1",
                people=["Tom Evans", "Aisha Khan", "Priya Shah"],
            ),
            Item(
                category=single.category,
                facts=single.facts,
                source_message_ids=["m01"],
                item_id="item-2",
                people=["Priya Shah", "Tom Evans"],
            ),
        ],
    )


def test_sources_add_sender_names_and_received_times_in_source_order() -> None:
    first = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    second = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    emails = [
        make_screened_email("m01", received=first),
        make_screened_email(
            "m02", sender_name="Tom Evans", sender_address="tom.evans@example.org", received=second
        ),
        make_screened_email("m03", received=second),
    ]
    item = make_item(source_message_ids=["m02", "m01", "m03"])

    [sourced] = with_sources([item], emails)

    assert sourced == SourcedItem(
        **item.model_dump(),
        sender_names=["Tom Evans", "Priya Shah"],
        received=[second, first, second],
    )


def _stored(*items: Item) -> Consolidation:
    return Consolidation(headline=HEADLINE, items=list(items))


def test_items_are_up_to_date_when_their_sources_are_the_included_records() -> None:
    consolidation = _stored(make_item(source_message_ids=["m01", "m02"]))

    assert items_up_to_date(consolidation, [INCLUDED, SECOND, EXCLUDED])


def test_nothing_to_consolidate_is_up_to_date() -> None:
    assert items_up_to_date(None, [EXCLUDED])


def test_included_record_in_no_item_makes_items_out_of_date() -> None:
    consolidation = _stored(make_item(source_message_ids=["m01"]))

    assert not items_up_to_date(consolidation, [INCLUDED, SECOND])
    assert not items_up_to_date(None, [INCLUDED])


def test_item_from_a_record_now_excluded_makes_items_out_of_date() -> None:
    consolidation = _stored(make_item(source_message_ids=["m01", "m03"]))

    assert not items_up_to_date(consolidation, [INCLUDED, EXCLUDED])


def test_restored_record_is_included_in_the_items() -> None:
    record = restored([EXCLUDED], "excluded-1", REVIEWER, section="Shout-outs", sections=SECTIONS)

    assert not items_up_to_date(_stored(make_item(source_message_ids=["m01"])), [INCLUDED, record])
    assert consolidation_input([INCLUDED, record]) == [INCLUDED, record]


def test_sender_names_are_each_sender_once_in_order() -> None:
    emails = [
        make_screened_email("m01", sender_name="Tom Evans"),
        make_screened_email("m02"),
        make_screened_email("m03", sender_name="Tom Evans"),
    ]

    assert sender_names(emails) == ["Tom Evans", "Priya Shah"]


def test_extracted_news_joins_the_facts_in_line_order() -> None:
    output = ExtractorOutput.model_validate(
        {
            "category": "customer_win",
            "exclusion_reason": None,
            "summary": "Northwind Retail signed.",
            "people": ["Priya Shah"],
            "lines": [
                {"line": 2, "facts": ["Onboarding starts in October."]},
                {"line": 0, "facts": []},
                {
                    "line": 1,
                    "facts": ["Priya Shah signed Northwind Retail.", "It is for three years."],
                },
            ],
        }
    )

    assert extracted_news(output) == ExtractedNews(
        category="customer_win",
        exclusion_reason=None,
        summary="Northwind Retail signed.",
        people=["Priya Shah"],
        facts=[
            "Priya Shah signed Northwind Retail.",
            "It is for three years.",
            "Onboarding starts in October.",
        ],
    )


FACT = "A 15 per cent surcharge applies from 3 October 2026, except for schools (1 December 2026)."
SPLIT = [
    "A 15 per cent surcharge applies from 3 October 2026.",
    "For schools, it applies from 1 December 2026.",
]


def test_added_numbers_are_those_the_source_does_not_hold_each_once() -> None:
    facts = ["Signed on 22 September 2026.", "Live on 1 May 2026, 1 week early."]

    assert added_numbers(facts, "Signed on 22 September 2026.") == ["1"]
    assert added_numbers(facts, "1 May 2026 and 22 September") == []


def test_split_facts_keeping_every_number_have_no_number_problems() -> None:
    assert number_problems([FACT], SPLIT, FACT) == []
    assert number_problems([FACT], [FACT], FACT) == []


def test_a_number_dropped_or_added_is_a_problem() -> None:
    dropped = ["A surcharge applies from 3 October 2026.", "For schools, from 1 December 2026."]
    added = [*SPLIT, "It ends on 31 March 2027."]

    assert number_problems([FACT], dropped, FACT) == ["dropped 15"]
    assert number_problems([FACT], added, FACT) == ["added 31", "added 2027"]


def test_a_number_from_the_line_may_be_added() -> None:
    line = "Woodgrove Branch (WG-400s): a 15% surcharge from 3 October 2026."
    before = ["A 15 per cent surcharge applies to Woodgrove Branch from 3 October 2026."]
    after = ["A 15 per cent surcharge applies to Woodgrove Branch (WG-400s) from 3 October 2026."]

    assert number_problems(before, after, line) == []


def test_no_facts_back_for_facts_given_is_a_problem() -> None:
    assert number_problems([FACT], [], FACT) == [
        "no facts",
        "dropped 15",
        "dropped 3",
        "dropped 2026",
        "dropped 1",
    ]
