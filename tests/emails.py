"""Synthetic inbox messages, and the submissions and extractor outputs derived from them."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from pulse.entities.extracts import ExtractorOutput
from pulse.entities.mail import InboundEmail
from pulse.entities.submissions import Submission, screen

ALLOWED_DOMAINS = ["techary.ai"]
CORPUS = Path(__file__).parent / "corpus" / "corpus.yaml"


def make_email(message_id: str = "m01", **changes: object) -> InboundEmail:
    fields: dict[str, object] = {
        "message_id": message_id,
        "sender_name": "Priya Shah",
        "sender_address": "priya.shah@techary.ai",
        "subject": "Signed Northwind Retail today",
        "received": datetime(2026, 9, 22, 15, 30, tzinfo=UTC),
        "has_attachments": False,
        "body": "Tom Evans and I signed Northwind Retail on 22 September.",
        "headers": {},
    }
    return InboundEmail.model_validate(fields | changes)


def screen_email(email: InboundEmail) -> Submission:
    """The pre-filter as main configures it for tests."""
    return screen(email, ALLOWED_DOMAINS, [], [])


def make_submission(message_id: str = "m01", **changes: object) -> Submission:
    return screen_email(make_email(message_id, **changes))


def make_output(message_id: str = "m01", **changes: object) -> ExtractorOutput:
    fields: dict[str, object] = {
        "message_id": message_id,
        "is_update": True,
        "exclusion_reason": None,
        "category": "customer_win",
        "summary": "Northwind Retail signed.",
        "facts": ["Signed Northwind Retail on 22 September"],
        "people": ["Priya Shah", "Tom Evans"],
        "sensitivity": [],
    }
    return ExtractorOutput.model_validate(fields | changes)


def corpus_messages() -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))["messages"]
    return messages


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
