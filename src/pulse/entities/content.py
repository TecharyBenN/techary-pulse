"""Newsletter content, the working draft, versions, entry notes and the draft checks."""

import re
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, PositiveInt

from pulse.entities.base import Entity, StrictEntity
from pulse.entities.errors import Refusal
from pulse.entities.extracts import ExtractRecord, Item, Sensitivity, sender_names
from pulse.entities.mail import ScreenedEmail

CheckName = Literal["word_count", "dashes", "digits", "people", "sentences", "items", "categories"]

_DASHES = ("\N{EN DASH}", "\N{EM DASH}")
_DIGITS = re.compile(r"[0-9]+")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_MAX_SENTENCES = 2

# The sensitivity flags of each flagged entry, by the entry's item ID.
EntryFlags = dict[str, list[Sensitivity]]
# The sender names each entry's credit line names, by the entry's item ID.
EntryCredits = dict[str, list[str]]


class Entry(StrictEntity):
    """One entry in a section: its text, its item and the people it names."""

    item_id: str
    text: str
    people: list[str]


class Section(StrictEntity):
    """A titled section of entries in one category."""

    category: str
    title: str
    entries: list[Entry]


class Content(StrictEntity):
    """The headline, intro and sections a working draft or version holds, as the writer structured
    them."""

    headline_title: str
    headline: str
    intro: str
    sections: list[Section]
    item_ids: list[str]

    def entries(self) -> Iterator[Entry]:
        for section in self.sections:
            yield from section.entries


class NotApplied(StrictEntity):
    """Feedback the writer did not apply, and why."""

    feedback: str
    reason: str


class WriterOutput(StrictEntity):
    """What the writer returns, stored as the working draft."""

    content: Content
    changes: list[str]
    not_applied: list[NotApplied]


class Claim(StrictEntity):
    """One claim a text makes, with the fact or feedback that states it."""

    claim: str
    # Quoted from the facts or feedback, or None when nothing states it or a fact contradicts it.
    source: str | None


class JudgeOutput(StrictEntity):
    """Each claim one text, the intro or an entry, makes, with its source."""

    claims: list[Claim]


class Verdict(JudgeOutput):
    """A judge's output with the text it is about, which code gives it."""

    # "intro", or the entry's item ID.
    target: str

    @property
    def supported(self) -> bool:
        return not self.unsupported()

    def unsupported(self) -> list[str]:
        """The claims nothing in the facts or feedback states."""
        return [claim.claim for claim in self.claims if claim.source is None]


class CheckFailure(Entity):
    """One failed draft check: the check, where it failed and the detail."""

    check: CheckName
    # "headline_title", "headline", "intro", an item ID, a category, or None for the whole draft.
    target: str | None
    detail: str


class EntryNotes(Entity):
    """What code adds to the writer's entries: credit lines for everyone, and flags for the
    reviewers."""

    credits: EntryCredits
    flags: EntryFlags


class Version(WriterOutput):
    """A working draft as presented to the reviewers."""

    version: PositiveInt
    created_at: AwareDatetime
    check_failures: list[CheckFailure]
    # None when the version was not judged.
    verdicts: list[Verdict] | None
    # Saved as presented, so they stay the same when the items change later.
    notes: EntryNotes


def version_of(
    draft: WriterOutput,
    number: int,
    created_at: datetime,
    check_failures: Sequence[CheckFailure],
    verdicts: Sequence[Verdict] | None,
    notes: EntryNotes,
) -> Version:
    """The working draft as the version it is presented as."""
    return Version(
        **draft.model_dump(),
        version=number,
        created_at=created_at,
        check_failures=list(check_failures),
        verdicts=None if verdicts is None else list(verdicts),
        notes=notes,
    )


class FlaggedEntry(Entity):
    """A flagged entry's text with its flags, as the flagged for review list shows it."""

    text: str
    flags: list[Sensitivity]


def entry_flags(
    content: Content, items: Sequence[Item], records: Sequence[ExtractRecord]
) -> EntryFlags:
    """Flag each entry with every sensitivity flag of its item's source records.

    Flags come from the records, never from the writer's output. Unflagged entries, and IDs
    that name no item, are left out.
    """
    by_message = {record.message_id: record for record in records}
    by_id = {item.item_id: item.source_message_ids for item in items}
    flags: EntryFlags = {}
    for entry in content.entries():
        found = [
            flag
            for message_id in by_id.get(entry.item_id, [])
            for flag in by_message[message_id].sensitivity
        ]
        if found:
            flags[entry.item_id] = found
    return flags


def entry_credits(
    content: Content, items: Sequence[Item], emails: Sequence[ScreenedEmail]
) -> EntryCredits:
    """Credit each included item to the senders of its source emails, each once."""
    return {
        item_id: sender_names(sources)
        for item_id, sources in item_sources(content, items, emails).items()
    }


def flagged_entries(content: Content, flags: EntryFlags) -> list[FlaggedEntry]:
    """The flagged entries in the order the newsletter shows them."""
    return [
        FlaggedEntry(text=entry.text, flags=flags[entry.item_id])
        for entry in content.entries()
        if entry.item_id in flags
    ]


def require_draft(draft: WriterOutput | None) -> WriterOutput:
    """The working draft, as the store returns it; refuse when there is none."""
    if draft is None:
        raise Refusal("there is no working draft")
    return draft


def item_sources(
    content: Content, items: Sequence[Item], emails: Sequence[ScreenedEmail]
) -> dict[str, list[ScreenedEmail]]:
    """Map each included item ID to its source emails.

    IDs that name no item are left out, so the checks can report them.
    """
    by_message = {email.message_id: email for email in emails}
    by_id = {item.item_id: item.source_message_ids for item in items}
    return {
        item_id: [by_message[message_id] for message_id in by_id[item_id]]
        for item_id in dict.fromkeys(content.item_ids)
        if item_id in by_id
    }


def source_text(email: ScreenedEmail) -> str:
    """The text names and numbers in a draft are checked against: the subject and the whole
    body, including any message it forwards or quotes."""
    return "\n".join(part for part in (email.subject, email.body) if part)


def draft_changed(draft: WriterOutput | None, latest: Version | None) -> bool:
    """Whether the working draft's content differs from the latest version's."""
    return draft is not None and (latest is None or draft.content != latest.content)


@dataclass(frozen=True)
class _Context:
    content: Content
    sources: Mapping[str, Sequence[ScreenedEmail]]
    feedback: Sequence[str]
    categories: Collection[str]
    max_words: int

    def texts(self, item_id: str) -> list[str]:
        return [source_text(email) for email in self.sources.get(item_id, ())]

    def senders(self, item_id: str) -> list[str]:
        return sender_names(self.sources.get(item_id, ()))

    def visible_text(self) -> Iterator[tuple[str | None, str]]:
        yield "headline_title", self.content.headline_title
        yield "headline", self.content.headline
        yield "intro", self.content.intro
        for section in self.content.sections:
            if section.entries:
                yield section.category, section.title
            for entry in section.entries:
                yield entry.item_id, entry.text


def check_content(
    content: Content,
    sources: Mapping[str, Sequence[ScreenedEmail]],
    feedback: Sequence[str],
    categories: Collection[str],
    max_words: int,
) -> list[CheckFailure]:
    """Return every check failure.

    `sources` maps each included item ID to its source emails.
    """
    context = _Context(content, sources, feedback, categories, max_words)
    return [failure for check in _CHECKS for failure in check(context)]


def _contains(text: str, part: str) -> bool:
    return part.casefold() in text.casefold()


def _in_any(texts: Sequence[str], part: str) -> bool:
    return any(_contains(text, part) for text in texts)


def _word_count(context: _Context) -> Iterator[CheckFailure]:
    words = sum(len(text.split()) for _, text in context.visible_text())
    if words > context.max_words:
        yield CheckFailure(
            check="word_count", target=None, detail=f"{words} words, limit {context.max_words}"
        )


def _dashes(context: _Context) -> Iterator[CheckFailure]:
    for target, text in context.visible_text():
        if any(dash in text for dash in _DASHES):
            yield CheckFailure(check="dashes", target=target, detail="em or en dash")


def _unsupported_digits(target: str, text: str, sources: Sequence[str]) -> Iterator[CheckFailure]:
    for digits in dict.fromkeys(_DIGITS.findall(text)):
        if not _in_any(sources, digits):
            yield CheckFailure(check="digits", target=target, detail=digits)


def _digits(context: _Context) -> Iterator[CheckFailure]:
    all_texts = [text for item_id in context.sources for text in context.texts(item_id)]
    yield from _unsupported_digits("intro", context.content.intro, [*all_texts, *context.feedback])
    for entry in context.content.entries():
        sources = [*context.texts(entry.item_id), *context.feedback]
        yield from _unsupported_digits(entry.item_id, entry.text, sources)


def _people(context: _Context) -> Iterator[CheckFailure]:
    for entry in context.content.entries():
        sources = [
            *context.texts(entry.item_id),
            *context.senders(entry.item_id),
            *context.feedback,
        ]
        # The writer's output checks already ensure every name is in the entry's text.
        for name in entry.people:
            if not _in_any(sources, name):
                yield CheckFailure(
                    check="people",
                    target=entry.item_id,
                    detail=f"{name} is not in the sources or feedback",
                )


def _sentences(context: _Context) -> Iterator[CheckFailure]:
    for entry in context.content.entries():
        count = sum(1 for part in _SENTENCE_END.split(entry.text) if part.strip())
        if count > _MAX_SENTENCES:
            yield CheckFailure(check="sentences", target=entry.item_id, detail=f"{count} sentences")


def _items(context: _Context) -> Iterator[CheckFailure]:
    included = context.content.item_ids
    for entry in context.content.entries():
        if entry.item_id not in included or entry.item_id not in context.sources:
            yield CheckFailure(check="items", target=entry.item_id, detail="not an included item")
    counts = Counter(entry.item_id for entry in context.content.entries())
    for item_id in dict.fromkeys(included):
        if counts[item_id] != 1:
            yield CheckFailure(
                check="items", target=item_id, detail=f"appears {counts[item_id]} times"
            )


def _categories(context: _Context) -> Iterator[CheckFailure]:
    for section in context.content.sections:
        if section.category not in context.categories:
            yield CheckFailure(
                check="categories", target=section.category, detail="not a configured category"
            )


_CHECKS: tuple[Callable[[_Context], Iterator[CheckFailure]], ...] = (
    _word_count,
    _dashes,
    _digits,
    _people,
    _sentences,
    _items,
    _categories,
)
