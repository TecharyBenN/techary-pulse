"""Extract records and the exclusion rules, and the items consolidated from them."""

import re
from collections.abc import Collection, Iterable, Sequence
from typing import Literal

from pydantic import AwareDatetime

from pulse.entities.base import StrictEntity
from pulse.entities.errors import Refusal
from pulse.entities.lifecycle import require_reviewer
from pulse.entities.mail import MessageId, ScreenedEmail

SensitivityKind = Literal[
    "named_person", "personal_information", "financial", "confidential", "inappropriate"
]
ExclusionReason = Literal["sensitivity", "no_category"]

_EXCLUDED_ID = re.compile(r"excluded-([0-9]+)")


class Sensitivity(StrictEntity):
    """A sensitivity flag the extractor raised: what it is, whether it must be withheld from
    all staff, and its evidence."""

    kind: SensitivityKind
    # The extractor's judgement; the kind never decides it.
    withheld: bool
    evidence: str


class ExtractorOutput(StrictEntity):
    """What the extractor finds in one screened email."""

    category: str | None
    # Free text for reviewers, given only when there is no category.
    exclusion_reason: str | None
    summary: str
    facts: list[str]
    people: list[str]
    sensitivity: list[Sensitivity]


def exclusion_outcome(output: ExtractorOutput) -> ExclusionReason | None:
    """The exclusion rules: why code excludes the record, or None when it is included."""
    if any(flag.withheld for flag in output.sensitivity):
        return "sensitivity"
    if output.category is None:
        return "no_category"
    return None


class ExtractRecord(ExtractorOutput):
    """An extractor output with the email it came from and the exclusion outcome code gave it."""

    message_id: MessageId
    exclusion: ExclusionReason | None
    # Reviewers restore an excluded record by this ID, so it never changes while excluded, and
    # a restored record keeps it.
    excluded_id: str | None
    # The reviewer who restored the record, which is then included.
    restored_by: str | None


def make_record(
    message_id: MessageId, output: ExtractorOutput, used_ids: Collection[str]
) -> ExtractRecord:
    """Apply the exclusion rules, numbering an excluded record after the excluded IDs in use
    within the newsletter, `used_ids`."""
    exclusion = exclusion_outcome(output)
    return ExtractRecord(
        **output.model_dump(),
        message_id=message_id,
        exclusion=exclusion,
        excluded_id=None if exclusion is None else f"excluded-{_highest(used_ids) + 1}",
        restored_by=None,
    )


def restored(
    records: Sequence[ExtractRecord], excluded_id: str, caller: str | None
) -> ExtractRecord:
    """The excluded record a reviewer named, now included; `caller` comes from the run, never
    from the model."""
    caller = require_reviewer(caller)
    record = next(
        (r for r in records if r.excluded_id == excluded_id and r.exclusion is not None), None
    )
    if record is None:
        raise Refusal(f"{excluded_id} names no excluded record in the open newsletter")
    return record.model_copy(update={"exclusion": None, "restored_by": caller})


def _highest(used_ids: Collection[str]) -> int:
    numbers = (_EXCLUDED_ID.fullmatch(excluded_id) for excluded_id in used_ids)
    return max((int(match.group(1)) for match in numbers if match), default=0)


class ConsolidatorItem(StrictEntity):
    """One piece of news as the consolidator merges it from the records that report it."""

    category: str
    facts: list[str]
    source_message_ids: list[MessageId]


class ConsolidatorOutput(StrictEntity):
    """The consolidator's items and headline."""

    headline: str
    items: list[ConsolidatorItem]


class Item(ConsolidatorItem):
    """A consolidated item with the ID and people code gave it."""

    item_id: str
    people: list[str]


class Consolidation(StrictEntity):
    """The newsletter's current items and headline."""

    headline: str
    items: list[Item]


class SourcedItem(Item):
    """An item with the sender names and received times of its source emails."""

    sender_names: list[str]
    received: list[AwareDatetime]


def consolidation_input(records: Sequence[ExtractRecord]) -> list[ExtractRecord]:
    """The included records; refuse when there are none."""
    included = [record for record in records if record.exclusion is None]
    if not included:
        raise Refusal("the newsletter has no included extract records")
    return included


def make_consolidation(
    output: ConsolidatorOutput, records: Sequence[ExtractRecord]
) -> Consolidation:
    """Number the items in order, and give each the people of its source records.

    `records` holds the consolidator's input, which includes every source.
    """
    by_id = {record.message_id: record for record in records}
    items = []
    for n, item in enumerate(output.items, start=1):
        sources = [by_id[message_id] for message_id in item.source_message_ids]
        people = list(dict.fromkeys(name for source in sources for name in source.people))
        items.append(Item(**item.model_dump(), item_id=f"item-{n}", people=people))
    return Consolidation(headline=output.headline, items=items)


def items_up_to_date(consolidation: Consolidation | None, records: Sequence[ExtractRecord]) -> bool:
    """Whether the items are built from exactly the included records.

    New extractions and restored records both leave the items out of date.
    """
    included = {record.message_id for record in records if record.exclusion is None}
    items = consolidation.items if consolidation else []
    return {message_id for item in items for message_id in item.source_message_ids} == included


def sender_names(emails: Iterable[ScreenedEmail]) -> list[str]:
    """Each sender's name once, in the order of the emails."""
    return list(dict.fromkeys(email.sender_name for email in emails))


def with_sources(items: Sequence[Item], emails: Sequence[ScreenedEmail]) -> list[SourcedItem]:
    """`emails` holds the newsletter's screened emails, which include every item source."""
    by_id = {email.message_id: email for email in emails}
    sourced = []
    for item in items:
        sources = [by_id[message_id] for message_id in item.source_message_ids]
        sourced.append(
            SourcedItem(
                **item.model_dump(),
                sender_names=sender_names(sources),
                received=[source.received for source in sources],
            )
        )
    return sourced
