"""Extract records and the exclusion rules."""

import re
from collections.abc import Collection
from typing import Literal

from pulse.entities.base import StrictEntity

SensitivityType = Literal["commercial", "personal", "unannounced", "inappropriate"]
ExtractorExclusion = Literal["not_an_update", "unclear", "no_matching_section"]
ExclusionReason = Literal["sensitivity", ExtractorExclusion]

_EXCLUDED_ID = re.compile(r"excluded-([0-9]+)")


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


class ExtractRecord(ExtractorOutput):
    """An extractor output with the exclusion outcome code gave it."""

    exclusion: ExclusionReason | None
    # Reviewers restore an excluded record by this ID, so it never changes while excluded.
    excluded_id: str | None


def make_record(
    output: ExtractorOutput, previous: ExtractRecord | None, used_ids: Collection[str]
) -> ExtractRecord:
    """Apply the exclusion rules, keeping the excluded ID of the record this one replaces.

    `used_ids` holds the excluded IDs in use within the newsletter.
    """
    exclusion = exclusion_outcome(output)
    excluded_id = None
    if exclusion is not None:
        excluded_id = previous.excluded_id if previous else None
        excluded_id = excluded_id or f"excluded-{_highest(used_ids) + 1}"
    return ExtractRecord(**output.model_dump(), exclusion=exclusion, excluded_id=excluded_id)


def _highest(used_ids: Collection[str]) -> int:
    numbers = (_EXCLUDED_ID.fullmatch(excluded_id) for excluded_id in used_ids)
    return max((int(match.group(1)) for match in numbers if match), default=0)
