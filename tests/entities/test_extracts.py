from datetime import UTC, datetime
from typing import Any

import pytest

from pulse.entities.errors import Refusal
from pulse.entities.extracts import (
    Consolidation,
    ExtractorOutput,
    ExtractRecord,
    Item,
    Sensitivity,
    SourcedItem,
    consolidation_input,
    exclusion_outcome,
    items_up_to_date,
    make_consolidation,
    make_record,
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
)

FLAG = Sensitivity(type="commercial", evidence="mentions annual contract value")


def test_included_record_has_no_outcome() -> None:
    assert exclusion_outcome(make_output()) is None


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"sensitivity": [FLAG]}, "sensitivity"),
        ({"sensitivity": [FLAG], "category": None}, "sensitivity"),
        ({"category": None}, "no_category"),
    ],
)
def test_exclusion(changes: dict[str, Any], expected: str) -> None:
    assert exclusion_outcome(make_output(**changes)) == expected


def test_output_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        make_output(confidence=0.9)


def test_record_rejects_unknown_sensitivity_type() -> None:
    with pytest.raises(ValueError):
        Sensitivity(type="secret", evidence="x")


def _record(
    output: ExtractorOutput, previous: ExtractRecord | None = None, *used: str
) -> ExtractRecord:
    return make_record("m01", output, previous, used)


def test_included_record_has_no_excluded_id() -> None:
    record = _record(make_output())

    assert (record.exclusion, record.excluded_id) == (None, None)


def test_record_keeps_the_extractor_output_and_takes_the_message_id_from_code() -> None:
    output = make_output()

    record = _record(output)

    assert record.message_id == "m01"
    assert (
        ExtractorOutput.model_validate(record.model_dump(include=set(ExtractorOutput.model_fields)))
        == output
    )


def test_first_excluded_record_is_numbered_from_one() -> None:
    record = _record(make_output(category=None))

    assert (record.exclusion, record.excluded_id) == ("no_category", "excluded-1")


def test_next_excluded_record_follows_the_highest_number() -> None:
    record = _record(make_output(category=None), None, "excluded-1", "excluded-3")

    assert record.excluded_id == "excluded-4"


def test_replaced_record_keeps_its_excluded_id_while_excluded() -> None:
    previous = _record(make_output(category=None), None, "excluded-1")

    record = _record(make_output(sensitivity=[FLAG]), previous, "excluded-1", "excluded-2")

    assert (record.exclusion, record.excluded_id) == ("sensitivity", "excluded-2")


def test_replaced_record_that_is_included_loses_its_excluded_id() -> None:
    previous = _record(make_output(category=None))

    assert _record(make_output(), previous, "excluded-1").excluded_id is None


def test_record_excluded_again_after_inclusion_gets_a_new_number() -> None:
    previous = _record(make_output())

    assert _record(make_output(category=None), previous, "excluded-1").excluded_id == "excluded-2"


INCLUDED = make_extract_record("m01")
SECOND = make_extract_record("m02", people=["Tom Evans", "Aisha Khan"])
EXCLUDED = make_extract_record("m03", category=None)


def test_input_is_the_named_included_records_once_each() -> None:
    records = [INCLUDED, SECOND, EXCLUDED]

    assert consolidation_input(records, ["m02", "m01", "m02"]) == [SECOND, INCLUDED]


def test_input_refuses_excluded_and_unextracted_records() -> None:
    with pytest.raises(Refusal) as refused:
        consolidation_input([INCLUDED, EXCLUDED], ["m01", "m03", "m99"])

    assert str(refused.value) == (
        "m03 is excluded as excluded-1; m99 has no extract record in the open newsletter"
    )


def test_input_refuses_when_no_records_are_named() -> None:
    with pytest.raises(Refusal, match="no extract records were named"):
        consolidation_input([INCLUDED], [])


def test_consolidation_numbers_items_in_order_and_gives_them_their_sources_people() -> None:
    merged = make_consolidator_item("m02", "m01")
    single = make_consolidator_item("m01", category="shout_out")

    consolidation = make_consolidation(make_consolidator_output(merged, single), [INCLUDED, SECOND])

    assert consolidation == Consolidation(
        headline=HEADLINE,
        items=[
            Item(
                **merged.model_dump(),
                item_id="item-1",
                people=["Tom Evans", "Aisha Khan", "Priya Shah"],
            ),
            Item(**single.model_dump(), item_id="item-2", people=["Priya Shah", "Tom Evans"]),
        ],
    )


def test_sources_add_sender_names_and_received_times_in_source_order() -> None:
    first = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    second = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    emails = [
        make_screened_email("m01", received=first),
        make_screened_email(
            "m02", sender_name="Tom Evans", sender_address="tom.evans@techary.ai", received=second
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
