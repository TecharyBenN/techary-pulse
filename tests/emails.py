"""Synthetic inbox messages for tests."""

from datetime import UTC, datetime

from pulse.entities.mail import InboundEmail


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
