from typing import Any

import pytest

from pulse.entities.extracts import ExtractorOutput, Sensitivity, exclusion_outcome


def _output(**changes: Any) -> ExtractorOutput:
    fields: dict[str, Any] = {
        "message_id": "m01",
        "is_update": True,
        "exclusion_reason": None,
        "category": "customer_win",
        "summary": "Northwind Retail signed.",
        "facts": ["Signed Northwind Retail on 22 September"],
        "people": ["Priya Shah", "Tom Evans"],
        "sensitivity": [],
    }
    return ExtractorOutput(**(fields | changes))


FLAG = Sensitivity(type="commercial", evidence="mentions annual contract value")


def test_included_record_has_no_outcome() -> None:
    assert exclusion_outcome(_output()) is None


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
    assert exclusion_outcome(_output(**changes)) == expected


def test_output_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        _output(confidence=0.9)


def test_record_rejects_unknown_sensitivity_type() -> None:
    with pytest.raises(ValueError):
        Sensitivity(type="secret", evidence="x")
