"""Extract records and the exclusion rules."""

from typing import Literal

from pulse.entities.base import StrictEntity

SensitivityType = Literal["commercial", "personal", "unannounced", "inappropriate"]
ExtractorExclusion = Literal["not_an_update", "unclear", "no_matching_section"]
ExclusionReason = Literal["sensitivity", ExtractorExclusion]


class Sensitivity(StrictEntity):
    type: SensitivityType
    evidence: str


class ExtractorOutput(StrictEntity):
    """What the extractor finds in one submission."""

    message_id: str
    is_update: bool
    exclusion_reason: ExtractorExclusion | None
    category: str | None
    summary: str
    facts: list[str]
    people: list[str]
    sensitivity: list[Sensitivity]


def exclusion_outcome(output: ExtractorOutput) -> ExclusionReason | None:
    """The exclusion rules: why code excludes the record, or None when it is included."""
    if output.sensitivity:
        return "sensitivity"
    if output.exclusion_reason is not None:
        return output.exclusion_reason
    if not output.is_update:
        return "not_an_update"
    if output.category is None:
        return "no_matching_section"
    return None
