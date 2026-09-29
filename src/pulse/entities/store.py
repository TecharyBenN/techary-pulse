"""The store interface: newsletters, feedback and conversation history."""

from typing import Protocol

from pulse.entities.base import Entity
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.lifecycle import Newsletter


class HistoryRow(Entity):
    """One saved model request or response, tagged with the message whose run produced it."""

    message_id: str
    data: bytes


class Store(Protocol):
    async def get_open_newsletter(self) -> Newsletter | None:
        """The newsletter that is neither sent nor abandoned, if there is one."""
        ...

    async def add_newsletter(self, newsletter: Newsletter) -> None: ...

    async def record_feedback(self, newsletter_id: str, message: ReviewerMessage) -> None:
        """Record a reviewer message; a message ID already recorded is left as it is."""
        ...

    async def load_history(self, newsletter_id: str) -> list[HistoryRow]:
        """The newsletter's saved history, in the order it was saved."""
        ...

    async def append_history(self, newsletter_id: str, message_id: str, data: bytes) -> None: ...
