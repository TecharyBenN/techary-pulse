"""Synthetic inbox messages, and the screened emails, extract records, items and drafts
derived from them."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from pulse.entities.content import Content, Entry, Section, Verdict, WriterOutput
from pulse.entities.extracts import (
    Consolidation,
    ConsolidatorItem,
    ConsolidatorOutput,
    ExtractorOutput,
    ExtractRecord,
    Item,
    make_record,
)
from pulse.entities.mail import InboundEmail, ScreenedEmail, screen
from pulse.entities.store import Store

# The fictional organisation's domain; example.com stands for outside senders.
STAFF_DOMAIN = "example.org"
ALLOWED_DOMAINS = [STAFF_DOMAIN]
CORPUS = Path(__file__).parent / "corpus" / "corpus.yaml"
HEADLINE = "A new retail customer"

# Written out by hand from the corpus cases, not derived from the rules under test.
CORPUS_OUTCOMES = {
    "m01": "included",
    "m02": "included",
    "m03": "included",
    "m04": "included",
    "m05": "included",
    "m06": "included",
    "m07": "rejected: automatic_reply",
    "m08": "excluded: no_category",
    "m09": "excluded: no_category",
    "m10": "excluded: no_category",
    "m11": "excluded: no_category",
    "m12": "excluded: no_category",
    "m13": "rejected: sender_domain",
    "m14": "rejected: sensitivity_label",
    "m15": "excluded: sensitivity",
    "m16": "excluded: sensitivity",
    "m17": "excluded: no_category",
    "m18": "excluded: no_category",
}
# The corpus's duplicate reports of one piece of news share an item.
CORPUS_ITEM_SOURCES = [["m01", "m02"], ["m03", "m06"], ["m04"], ["m05"]]


def make_email(message_id: str = "m01", **changes: object) -> InboundEmail:
    fields: dict[str, object] = {
        "message_id": message_id,
        "sender_name": "Priya Shah",
        "sender_address": "priya.shah@example.org",
        "subject": "Signed Northwind Retail today",
        "received": datetime(2026, 9, 22, 15, 30, tzinfo=UTC),
        "has_attachments": False,
        "body": "Tom Evans and I signed Northwind Retail on 22 September.",
        "headers": {},
    }
    return InboundEmail.model_validate(fields | changes)


def screen_email(email: InboundEmail) -> ScreenedEmail:
    """The pre-filter as main configures it for tests."""
    return screen(email, ALLOWED_DOMAINS, [], [])


def make_screened_email(message_id: str = "m01", **changes: object) -> ScreenedEmail:
    return screen_email(make_email(message_id, **changes))


def make_output(**changes: object) -> ExtractorOutput:
    """An included output by default; `category=None` also gives it a reason, unless one is set."""
    if changes.get("category", "") is None:
        changes = {"exclusion_reason": "Fits no section."} | changes
    fields: dict[str, object] = {
        "exclusion_reason": None,
        "category": "customer_win",
        "summary": "Northwind Retail signed.",
        "facts": ["Signed Northwind Retail on 22 September"],
        "people": ["Priya Shah", "Tom Evans"],
        "sensitivity": [],
    }
    return ExtractorOutput.model_validate(fields | changes)


def make_extract_record(message_id: str = "m01", **changes: object) -> ExtractRecord:
    """A first extraction of the email, numbered as the only record in its newsletter."""
    return make_record(message_id, make_output(**changes), None, [])


def make_consolidator_item(*source_message_ids: str, **changes: object) -> ConsolidatorItem:
    fields: dict[str, object] = {
        "category": "customer_win",
        "facts": ["Signed Northwind Retail on 22 September"],
        "source_message_ids": list(source_message_ids or ["m01"]),
    }
    return ConsolidatorItem.model_validate(fields | changes)


def make_consolidator_output(*items: ConsolidatorItem) -> ConsolidatorOutput:
    return ConsolidatorOutput(headline=HEADLINE, items=list(items))


def make_item(item_id: str = "item-1", **changes: object) -> Item:
    fields: dict[str, object] = make_consolidator_item().model_dump() | {
        "item_id": item_id,
        "people": ["Priya Shah", "Tom Evans"],
    }
    return Item.model_validate(fields | changes)


def make_consolidation(*items: Item) -> Consolidation:
    return Consolidation(headline=HEADLINE, items=list(items))


ENTRY = "Priya Shah and Tom Evans signed Northwind Retail on 22 September."


def make_draft(*entries: Entry, **changes: object) -> WriterOutput:
    """A draft of one customer win entry for item-1, by default."""
    entries = entries or (Entry(item_id="item-1", text=ENTRY, people=["Priya Shah", "Tom Evans"]),)
    content = Content(
        headline_title="Headline of the week",
        headline=HEADLINE,
        intro="A strong week for new customers.",
        sections=[Section(category="customer_win", title="Customer wins", entries=list(entries))],
        item_ids=list(dict.fromkeys(entry.item_id for entry in entries)),
    )
    fields: dict[str, object] = {"content": content, "changes": [], "not_applied": []}
    return WriterOutput.model_validate(fields | changes)


def make_verdict(target: str = "item-1", claim: str | None = None) -> Verdict:
    """A supported verdict, unless it names an unsupported claim."""
    return Verdict(target=target, supported=claim is None, claim=claim)


def corpus_messages() -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = _corpus()["messages"]
    return messages


def corpus_consolidation() -> ConsolidatorOutput:
    return ConsolidatorOutput.model_validate(_corpus()["consolidation"])


def seedable_corpus_messages() -> list[dict[str, Any]]:
    """The corpus messages placed in the dev inbox for live tests. The others need headers Graph
    will not set, or are marked `seed: false`, so only the unit tests cover them."""
    return [m for m in corpus_messages() if "headers" not in m and m.get("seed", True)]


async def stored_outcomes(store: Store, newsletter_id: str) -> dict[str, str]:
    """Each screened email's outcome, by message ID, in the form CORPUS_OUTCOMES uses."""
    records = {r.message_id: r for r in await store.list_extract_records(newsletter_id)}
    outcomes = {}
    for email in await store.list_screened_emails(newsletter_id):
        record = records.get(email.message_id)
        if email.rejection is not None:
            outcomes[email.message_id] = f"rejected: {email.rejection}"
        elif record is None:
            outcomes[email.message_id] = "not extracted"
        elif record.exclusion is None:
            outcomes[email.message_id] = "included"
        else:
            outcomes[email.message_id] = f"excluded: {record.exclusion}"
    return outcomes


def _corpus() -> dict[str, Any]:
    corpus: dict[str, Any] = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    return corpus


def corpus_email(message: dict[str, Any], position: int) -> InboundEmail:
    """A corpus message as listed from the inbox, received in corpus order."""
    return make_email(
        message["id"],
        sender_name=message["sender_name"],
        sender_address=message["sender_address"],
        subject=message["subject"],
        body=message["body"],
        received=datetime(2026, 9, 22, 9, 0, tzinfo=UTC) + timedelta(minutes=position),
        has_attachments=message.get("has_attachments", False),
        headers=message.get("headers", {}),
    )
