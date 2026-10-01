"""Newsletter content, the working draft and its versions, and the draft checks."""

import re
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, PositiveInt

from pulse.entities.base import Entity, StrictEntity
from pulse.entities.extracts import ExtractRecord, Item
from pulse.entities.mail import ScreenedEmail, sender_names, source_text

CheckName = Literal[
    "word_count", "dashes", "digits", "people", "sentences", "senders", "items", "categories"
]

_DASHES = ("\N{EN DASH}", "\N{EM DASH}")
_DIGITS = re.compile(r"[0-9]+")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_MAX_SENTENCES = 2


class Entry(StrictEntity):
    item_id: str
    text: str
    people: list[str]


class Section(StrictEntity):
    category: str
    title: str
    entries: list[Entry]


class Content(StrictEntity):
    """What a working draft or version holds, and what is rendered and sent, as the writer
    structured it."""

    headline_title: str
    headline: str
    intro: str
    sections: list[Section]
    item_ids: list[str]


class NotApplied(StrictEntity):
    feedback: str
    reason: str


class WriterOutput(StrictEntity):
    """What the writer returns, stored as the working draft."""

    content: Content
    changes: list[str]
    not_applied: list[NotApplied]


class Version(WriterOutput):
    """A working draft as presented to the reviewers."""

    version: PositiveInt
    created_at: AwareDatetime


def version_of(draft: WriterOutput, number: int, created_at: datetime) -> Version:
    """The working draft as the version it is presented as."""
    return Version(**draft.model_dump(), version=number, created_at=created_at)


def item_sources(
    content: Content,
    items: Sequence[Item],
    records: Sequence[ExtractRecord],
    emails: Sequence[ScreenedEmail],
) -> dict[str, list[ScreenedEmail]]:
    """Map each included item ID, or restored record ID, to its source emails.

    IDs that name no item or excluded record are left out, so the checks can report them.
    """
    by_message = {email.message_id: email for email in emails}
    by_id = {item.item_id: item.source_message_ids for item in items} | {
        record.excluded_id: [record.message_id] for record in records if record.excluded_id
    }
    return {
        item_id: [by_message[message_id] for message_id in by_id[item_id]]
        for item_id in dict.fromkeys(content.item_ids)
        if item_id in by_id
    }


def draft_changed(draft: WriterOutput | None, latest: Version | None) -> bool:
    """Whether the working draft's content differs from the latest version's."""
    return draft is not None and (latest is None or draft.content != latest.content)


class CheckFailure(Entity):
    check: CheckName
    # "headline_title", "headline", "intro", an item ID, a category, or None for the whole draft.
    target: str | None
    detail: str


@dataclass(frozen=True)
class _Context:
    content: Content
    sources: Mapping[str, Sequence[ScreenedEmail]]
    feedback: Sequence[str]
    categories: Collection[str]
    max_words: int

    def entries(self) -> Iterator[Entry]:
        for section in self.content.sections:
            yield from section.entries

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

    `sources` maps each included item ID, or restored record ID, to its source emails.
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
    for entry in context.entries():
        sources = [*context.texts(entry.item_id), *context.feedback]
        yield from _unsupported_digits(entry.item_id, entry.text, sources)


def _people(context: _Context) -> Iterator[CheckFailure]:
    for entry in context.entries():
        sources = [
            *context.texts(entry.item_id),
            *context.senders(entry.item_id),
            *context.feedback,
        ]
        for name in entry.people:
            if not _contains(entry.text, name):
                yield CheckFailure(
                    check="people", target=entry.item_id, detail=f"{name} is not in the entry"
                )
            if not _in_any(sources, name):
                yield CheckFailure(
                    check="people",
                    target=entry.item_id,
                    detail=f"{name} is not in the sources or feedback",
                )


def _sentences(context: _Context) -> Iterator[CheckFailure]:
    for entry in context.entries():
        count = sum(1 for part in _SENTENCE_END.split(entry.text) if part.strip())
        if count > _MAX_SENTENCES:
            yield CheckFailure(check="sentences", target=entry.item_id, detail=f"{count} sentences")


def _senders(context: _Context) -> Iterator[CheckFailure]:
    for entry in context.entries():
        for sender in context.senders(entry.item_id):
            if not _contains(entry.text, sender):
                yield CheckFailure(
                    check="senders", target=entry.item_id, detail=f"{sender} is not named"
                )


def _items(context: _Context) -> Iterator[CheckFailure]:
    included = context.content.item_ids
    for entry in context.entries():
        if entry.item_id not in included or entry.item_id not in context.sources:
            yield CheckFailure(check="items", target=entry.item_id, detail="not an included item")
    counts = Counter(entry.item_id for entry in context.entries())
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


# Add a check by writing its function and listing it here.
_CHECKS: tuple[Callable[[_Context], Iterator[CheckFailure]], ...] = (
    _word_count,
    _dashes,
    _digits,
    _people,
    _sentences,
    _senders,
    _items,
    _categories,
)
