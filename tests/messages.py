"""Synthetic reviewer messages and newsletters for tests."""

from datetime import UTC, datetime

from pulse.entities.conversation import ReviewerMessage
from pulse.entities.lifecycle import Newsletter, open_newsletter

REVIEWER = "testuser@techary.ai"
OPENED = datetime(2026, 9, 25, 16, 30, tzinfo=UTC)


def make_message(message_id: str = "r01", **changes: object) -> ReviewerMessage:
    fields: dict[str, object] = {
        "message_id": message_id,
        "author": REVIEWER,
        "channel": "librechat",
        "text": "Please make the intro shorter.",
        "received": datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
    }
    return ReviewerMessage.model_validate(fields | changes)


def make_newsletter(newsletter_id: str = "n-1") -> Newsletter:
    return open_newsletter(newsletter_id, OPENED)
