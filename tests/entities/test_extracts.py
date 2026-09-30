from typing import Any

import pytest

from pulse.entities.extracts import (
    ExtractorOutput,
    ExtractRecord,
    Sensitivity,
    exclusion_outcome,
    make_record,
)
from tests.emails import make_output

FLAG = Sensitivity(type="commercial", evidence="mentions annual contract value")


def test_included_record_has_no_outcome() -> None:
    assert exclusion_outcome(make_output()) is None


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"sensitivity": [FLAG]}, "sensitivity"),
        ({"sensitivity": [FLAG], "exclusion_reason": "unclear"}, "sensitivity"),
        (
            {"is_update": False, "exclusion_reason": "not_an_update", "category": None},
            "not_an_update",
        ),
        ({"exclusion_reason": "unclear", "category": None}, "unclear"),
        ({"exclusion_reason": "no_matching_section", "category": None}, "no_matching_section"),
        ({"is_update": False, "category": None}, "not_an_update"),
        ({"is_update": False}, "not_an_update"),
        ({"category": None}, "no_matching_section"),
        ({"exclusion_reason": "unclear"}, "unclear"),
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
    return make_record(output, previous, used)


def test_included_record_has_no_excluded_id() -> None:
    record = _record(make_output())

    assert (record.exclusion, record.excluded_id) == (None, None)


def test_record_keeps_the_extractor_output() -> None:
    output = make_output()

    assert (
        ExtractorOutput.model_validate(
            _record(output).model_dump(include=set(ExtractorOutput.model_fields))
        )
        == output
    )


def test_first_excluded_record_is_numbered_from_one() -> None:
    record = _record(make_output(category=None))

    assert (record.exclusion, record.excluded_id) == ("no_matching_section", "excluded-1")


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
