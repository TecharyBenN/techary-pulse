"""Extract records and the exclusion rules, and the items consolidated from them."""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Literal

from pydantic import AwareDatetime, ConfigDict

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
    """A sensitivity flag the sensitivity agent raised: what it is, whether it must be withheld
    from all staff, and its evidence."""

    # Each field's docstring is its description in the schema the sensitivity agent is given.
    model_config = ConfigDict(use_attribute_docstrings=True)

    kind: SensitivityKind
    """What the flagged content is."""

    # The sensitivity agent's judgement; the kind never decides it.
    withheld: bool
    """Whether the content must be kept from all staff until a reviewer decides otherwise."""

    evidence: str
    """A short phrase, in your own words, saying what triggered the flag."""


class News(StrictEntity):
    """The news in one email that a member of staff sent to Techary's newsletter, apart from its
    facts."""

    # Each field's docstring is its description in the schema the extractor is given.
    model_config = ConfigDict(use_attribute_docstrings=True)

    category: str | None
    """The category in <categories> that the news fits, or null when it fits none."""

    exclusion_reason: str | None
    """One short sentence for the newsletter's reviewers saying why the email has no category;
    null when it has one."""

    summary: str
    """One short sentence stating the news."""

    people: list[str]
    """The full name of every person the facts name."""


class LineFacts(StrictEntity):
    """The facts one line of the email states."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    line: int
    """The number of a line in <lines>."""

    facts: list[str]
    """The facts that line states, one short sentence each."""


class ExtractorOutput(News):
    """The extractor's answer for one email: its news, line by line."""

    lines: list[LineFacts]
    """Every line in <lines> with the facts it states."""


class ExtractedNews(News):
    """The news in one email with its facts in line order, as code joins them."""

    facts: list[str]
    """The facts of the news, one short sentence each."""


class DecomposerOutput(StrictEntity):
    """One line's facts, split so each states one thing."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    facts: list[str]
    """The facts, each stating one thing, one short sentence each."""


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def number_problems(before: Sequence[str], after: Sequence[str], source: str) -> list[str]:
    """How splitting `before` into `after` lost or invented a detail: a number of `before` that
    `after` drops, or a number of `after` that neither `before` nor `source`, the line the facts
    came from, holds."""
    given = dict.fromkeys(n for fact in before for n in _NUMBER.findall(fact))
    split = dict.fromkeys(n for fact in after for n in _NUMBER.findall(fact))
    known = set(given) | set(_NUMBER.findall(source))
    problems = ["no facts"] if before and not after else []
    problems += [f"dropped {n}" for n in given if n not in split]
    problems += [f"added {n}" for n in split if n not in known]
    return problems


def extracted_news(output: ExtractorOutput) -> ExtractedNews:
    """The extractor's answer with each line's facts joined in line order."""
    ordered = sorted(output.lines, key=lambda line: line.line)
    facts = [fact for line in ordered for fact in line.facts]
    return ExtractedNews(**output.model_dump(exclude={"lines"}), facts=facts)


class SensitivityOutput(StrictEntity):
    """What the sensitivity agent flags in one screened email."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    sensitivity: list[Sensitivity]
    """One flag for each sensitive thing in the email; empty when there is none."""


class Extraction(ExtractedNews, SensitivityOutput):
    """Both agents' answers for one screened email, as code combines them."""


def exclusion_outcome(extraction: Extraction) -> ExclusionReason | None:
    """The exclusion rules: why code excludes the record, or None when it is included."""
    if any(flag.withheld for flag in extraction.sensitivity):
        return "sensitivity"
    if extraction.category is None:
        return "no_category"
    return None


class ExtractRecord(Extraction):
    """An extraction with the email it came from and the exclusion outcome code gave it."""

    message_id: MessageId
    exclusion: ExclusionReason | None
    # Reviewers restore an excluded record by this ID, so it never changes while excluded, and
    # a restored record keeps it.
    excluded_id: str | None
    # The reviewer who restored the record, which is then included.
    restored_by: str | None


def make_record(
    message_id: MessageId, extraction: Extraction, used_ids: Collection[str]
) -> ExtractRecord:
    """Apply the exclusion rules, numbering an excluded record after the excluded IDs in use
    within the newsletter, `used_ids`."""
    exclusion = exclusion_outcome(extraction)
    return ExtractRecord(
        **extraction.model_dump(),
        message_id=message_id,
        exclusion=exclusion,
        excluded_id=None if exclusion is None else f"excluded-{_highest(used_ids) + 1}",
        restored_by=None,
    )


def restored(
    records: Sequence[ExtractRecord],
    excluded_id: str,
    caller: str | None,
    *,
    section: str | None = None,
    sections: Mapping[str, str],
) -> ExtractRecord:
    """The excluded record a reviewer named, now included; `caller` comes from the run, never
    from the model. A record with no category takes the category of `section`, the section the
    reviewer named; `sections` maps each configured category to its section title."""
    caller = require_reviewer(caller)
    record = next(
        (r for r in records if r.excluded_id == excluded_id and r.exclusion is not None), None
    )
    if record is None:
        raise Refusal(f"{excluded_id} names no excluded record in the open newsletter")
    if not record.facts:
        raise Refusal(
            f"{excluded_id} has no facts to write from; the reviewer can give the news in their "
            "feedback instead"
        )
    category = record.category
    if category is None:
        titles = ", ".join(sections.values())
        if section is None:
            raise Refusal(
                f"{excluded_id} fits no section, so the reviewer must name one of: {titles}"
            )
        by_title = {title.casefold(): category for category, title in sections.items()}
        category = by_title.get(section.strip().casefold())
        if category is None:
            raise Refusal(f"{section} is not a section; name one of: {titles}")
    return record.model_copy(
        update={"exclusion": None, "restored_by": caller, "category": category}
    )


def _highest(used_ids: Collection[str]) -> int:
    numbers = (_EXCLUDED_ID.fullmatch(excluded_id) for excluded_id in used_ids)
    return max((int(match.group(1)) for match in numbers if match), default=0)


class ItemContent(StrictEntity):
    """An item's news: its category and facts."""

    # Each field's docstring is its description in the schema the consolidator is given.
    model_config = ConfigDict(use_attribute_docstrings=True)

    category: str
    """The category of the item's records."""

    facts: list[str]
    """The item's facts, one short sentence each, each stated once."""


class ConsolidatorItem(ItemContent):
    """One piece of news as the consolidator merges it from the records that report it, named
    by the short record IDs code gave its input."""

    source_record_ids: list[str]
    """The record ID of every record merged into the item."""


class ConsolidatorOutput(StrictEntity):
    """The consolidator's items and headline."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    headline: str
    """One short line in sentence case summing up the newsletter's main news."""

    items: list[ConsolidatorItem]
    """One item for each piece of news."""


class Item(ItemContent):
    """A consolidated item with its source emails, and the ID and people code gave it."""

    source_message_ids: list[MessageId]
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


def numbered_records(records: Sequence[ExtractRecord]) -> dict[str, ExtractRecord]:
    """The consolidator's input records by the short IDs code gives them, in input order, so the
    model never copies a message ID."""
    return {f"record-{n}": record for n, record in enumerate(records, start=1)}


def make_consolidation(
    output: ConsolidatorOutput, records: Sequence[ExtractRecord]
) -> Consolidation:
    """Number the items in order, and give each the message IDs and people of its source
    records.

    `records` holds the consolidator's input, which includes every source.
    """
    by_id = numbered_records(records)
    items = []
    for n, item in enumerate(output.items, start=1):
        sources = [by_id[record_id] for record_id in item.source_record_ids]
        people = list(dict.fromkeys(name for source in sources for name in source.people))
        items.append(
            Item(
                category=item.category,
                facts=item.facts,
                source_message_ids=[source.message_id for source in sources],
                item_id=f"item-{n}",
                people=people,
            )
        )
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
