"""The store interface: newsletters, submissions, extract records, feedback and history."""

from collections.abc import Sequence
from typing import Protocol

from pulse.entities.base import Entity
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.extracts import ExtractorOutput, ExtractRecord
from pulse.entities.lifecycle import Newsletter
from pulse.entities.submissions import Submission


class HistoryRow(Entity):
    """One saved model request or response, tagged with the message whose run produced it."""

    message_id: str
    data: bytes


class Store(Protocol):
    async def get_open_newsletter(self) -> Newsletter | None:
        """The newsletter that is neither sent nor abandoned, if there is one."""
        ...

    async def save_start(self, newsletter: Newsletter, submissions: Sequence[Submission]) -> None:
        """Save a newsletter opened or updated by `start_newsletter`, with its new submissions.

        A submission already saved for the newsletter is left as it is.
        """
        ...

    async def list_submissions(self, newsletter_id: str) -> list[Submission]:
        """The newsletter's submissions, sorted by received time, then message ID."""
        ...

    async def save_extract(self, newsletter_id: str, output: ExtractorOutput) -> ExtractRecord:
        """Save the output as its submission's extract record, replacing any earlier one."""
        ...

    async def list_extract_records(self, newsletter_id: str) -> list[ExtractRecord]:
        """The newsletter's extract records, in the order they were first saved."""
        ...

    async def record_feedback(self, newsletter_id: str, message: ReviewerMessage) -> None:
        """Record a reviewer message; a message ID already recorded is left as it is."""
        ...

    async def load_history(self, newsletter_id: str) -> list[HistoryRow]:
        """The newsletter's saved history, in the order it was saved."""
        ...

    async def append_history(self, newsletter_id: str, message_id: str, data: bytes) -> None: ...
