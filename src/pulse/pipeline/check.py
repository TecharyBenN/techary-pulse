"""Step 7: the code checks on a draft, and the judge's verdicts as failures."""

import re
from collections.abc import Sequence

from pulse.config import Config
from pulse.models import Draft, ItemWithSenders, JudgeResult, Message
from pulse.pipeline.render import visible_text

EN_DASH, EM_DASH = chr(0x2013), chr(0x2014)
_DIGITS = re.compile(r"\d+")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _source_text(messages: Sequence[Message]) -> str:
    return "\n".join(f"{m.subject}\n{m.body}" for m in messages)


def _sentences(text: str) -> int:
    return len([part for part in _SENTENCE_END.split(text) if part.strip()])


def check_draft(
    draft: Draft,
    headline: str,
    items: Sequence[ItemWithSenders],
    messages: dict[str, Message],
    config: Config,
) -> list[str]:
    """Return every failure of the design's code checks; empty if the draft passes."""
    failures: list[str] = []
    by_id = {item.item_id: item for item in items}
    words = sum(len(text.split()) for text in visible_text(config, headline, draft))
    if words > config.limits.max_words:
        failures.append(f"newsletter is {words} words, more than {config.limits.max_words}")

    texts = [draft.intro, *(e.text for s in draft.sections for e in s.entries)]
    if any(EN_DASH in text or EM_DASH in text for text in texts):
        failures.append("draft uses an em dash or en dash")

    all_sources = _source_text([messages[i] for item in items for i in item.source_message_ids])
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
            sources = _source_text([messages[i] for i in item.source_message_ids])
            text = entry.text.casefold()
            for number in _DIGITS.findall(entry.text):
                if number not in sources:
                    failures.append(f"{item.item_id}: number {number} is not in its sources")
            for name in entry.people:
                if name.casefold() not in text:
                    failures.append(f"{item.item_id}: {name} is listed but not named")
                named = name.casefold() in sources.casefold() or name in item.sender_names
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
