"""Step 7: the code checks on a draft, and the judge's verdicts as failures."""

import re
from collections.abc import Mapping, Sequence

from pulse.config import Config
from pulse.models import Draft, ItemWithSenders, JudgeResult
from pulse.pipeline.render import visible_text
from pulse.store import SourceRow

EN_DASH, EM_DASH = chr(0x2013), chr(0x2014)
_DIGITS = re.compile(r"\d+")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _source_text(sources: Sequence[SourceRow]) -> str:
    return "\n".join(f"{s.subject}\n{s.body or ''}" for s in sources)


def _sentences(text: str) -> int:
    return len([part for part in _SENTENCE_END.split(text) if part.strip()])


def check_draft(
    draft: Draft,
    headline: str,
    items: Sequence[ItemWithSenders],
    sources: Mapping[str, SourceRow],
    config: Config,
    feedback: Sequence[str] = (),
) -> list[str]:
    """Return every failure of the design's code checks; empty if the draft passes.

    A digit or name is also allowed to come from `feedback`, so a revision can state a fact a
    reviewer gave directly rather than repeating one already in a source message.
    """
    failures: list[str] = []
    by_id = {item.item_id: item for item in items}
    words = sum(len(text.split()) for text in visible_text(config, headline, draft))
    if words > config.limits.max_words:
        failures.append(f"newsletter is {words} words, more than {config.limits.max_words}")

    texts = [draft.intro, *(e.text for s in draft.sections for e in s.entries)]
    if any(EN_DASH in text or EM_DASH in text for text in texts):
        failures.append("draft uses an em dash or en dash")

    feedback_text = "\n".join(feedback)
    all_sources = (
        _source_text([sources[i] for item in items for i in item.source_message_ids])
        + "\n"
        + feedback_text
    )
    for number in _DIGITS.findall(draft.intro):
        if number not in all_sources:
            failures.append(f"intro number {number} does not appear in a source message")

    categories = {section.category for section in config.sections}
    seen: list[str] = []
    for section in draft.sections:
        if section.category not in categories:
            failures.append(f"category {section.category} is not configured")
        for entry in section.entries:
            item = by_id.get(entry.item_id)
            if item is None:
                failures.append(f"entry references unknown item {entry.item_id}")
                continue
            seen.append(entry.item_id)
            item_sources = (
                _source_text([sources[i] for i in item.source_message_ids]) + "\n" + feedback_text
            )
            text = entry.text.casefold()
            for number in _DIGITS.findall(entry.text):
                if number not in item_sources:
                    failures.append(f"{item.item_id}: number {number} is not in its sources")
            for name in entry.people:
                if name.casefold() not in text:
                    failures.append(f"{item.item_id}: {name} is listed but not named")
                named = name.casefold() in item_sources.casefold() or name in item.sender_names
                if not named:
                    failures.append(f"{item.item_id}: {name} is not in its sources or senders")
            if _sentences(entry.text) > 2:
                failures.append(f"{item.item_id}: entry is more than two sentences")
            for sender in item.sender_names:
                if sender.casefold() not in text:
                    failures.append(f"{item.item_id}: sender {sender} is not named")

    for item_id in by_id:
        count = seen.count(item_id)
        if count != 1:
            failures.append(f"item {item_id} appears {count} times, not once")
    return failures


def judge_failures(result: JudgeResult) -> list[str]:
    failures = [] if result.intro.supported else [f"intro: {result.intro.reason}"]
    failures += [f"{v.item_id}: {v.reason}" for v in result.entries if not v.supported]
    return failures
