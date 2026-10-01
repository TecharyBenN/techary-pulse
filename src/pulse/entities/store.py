"""The store interface: newsletters, screened emails, extract records, items, drafts, versions,
feedback, history and handled messages."""

from collections.abc import Sequence
from typing import Protocol

from pulse.entities.base import Entity
from pulse.entities.content import Verdict, Version, WriterOutput
from pulse.entities.conversation import HandledMessage, ReviewerMessage
from pulse.entities.extracts import Consolidation, ExtractorOutput, ExtractRecord
from pulse.entities.lifecycle import Newsletter
from pulse.entities.mail import MessageId, ScreenedEmail

# The tag on delivery's note in a newsletter's history, in place of a reviewer message ID.
DELIVERY_NOTE = "delivery"


class HistoryRow(Entity):
    """One saved model request or response, tagged with the message whose run produced it."""

    message_id: str
    data: bytes


class Store(Protocol):
    async def get_open_newsletter(self) -> Newsletter | None:
        """The newsletter that is neither sent nor abandoned, if there is one."""
        ...

    async def get_latest_newsletter(self) -> Newsletter | None:
        """The newsletter opened most recently, open or closed, if there is one."""
        ...

    async def save_newsletter(self, newsletter: Newsletter) -> None:
        """Save a newsletter's state, replacing what was saved before."""
        ...

    async def save_start(self, newsletter: Newsletter, emails: Sequence[ScreenedEmail]) -> None:
        """Save a newsletter opened or updated by `start_newsletter`, with its new screened emails.

        An email already saved for the newsletter is left as it is.
        """
        ...

    async def list_screened_emails(self, newsletter_id: str) -> list[ScreenedEmail]:
        """The newsletter's screened emails, sorted by received time, then message ID."""
        ...

    async def mark_moved(self, newsletter_id: str, message_id: MessageId) -> None:
        """Record that delivery moved the screened email out of the inbox."""
        ...

    async def save_extract(
        self, newsletter_id: str, message_id: MessageId, output: ExtractorOutput
    ) -> ExtractRecord:
        """Save the output as the email's extract record, replacing any earlier one."""
        ...

    async def save_restored(self, newsletter_id: str, record: ExtractRecord) -> None:
        """Save a restored record in place of the excluded record it was."""
        ...

    async def list_extract_records(self, newsletter_id: str) -> list[ExtractRecord]:
        """The newsletter's extract records, in the order they were first saved."""
        ...

    async def save_items(self, newsletter_id: str, consolidation: Consolidation) -> None:
        """Save the newsletter's items and headline, replacing any earlier ones."""
        ...

    async def get_items(self, newsletter_id: str) -> Consolidation | None:
        """The newsletter's current items and headline, or None before it is consolidated."""
        ...

    async def save_draft(self, newsletter_id: str, draft: WriterOutput) -> None:
        """Save the newsletter's working draft, replacing any earlier one and its verdicts."""
        ...

    async def get_draft(self, newsletter_id: str) -> WriterOutput | None: ...

    async def save_verdicts(self, newsletter_id: str, verdicts: Sequence[Verdict]) -> None:
        """Save the judge's verdicts on the working draft, replacing any earlier ones."""
        ...

    async def get_verdicts(self, newsletter_id: str) -> list[Verdict] | None:
        """The judge's latest verdicts, or None when the working draft has not been judged."""
        ...

    async def save_version(self, newsletter: Newsletter, version: Version) -> None:
        """Save a presented version with the newsletter that numbers it, in one transaction."""
        ...

    async def get_version(self, newsletter_id: str, version: int) -> Version | None: ...

    async def record_feedback(self, newsletter_id: str, message: ReviewerMessage) -> None:
        """Record a reviewer message for the newsletter; a message ID already recorded for it is
        left as it is."""
        ...

    async def list_feedback(self, newsletter_id: str) -> list[ReviewerMessage]:
        """The newsletter's reviewer messages, in the order they were recorded."""
        ...

    async def load_history(self, newsletter_id: str) -> list[HistoryRow]:
        """The newsletter's saved history, in the order it was saved."""
        ...

    async def append_history(self, newsletter_id: str, message_id: str, data: bytes) -> None: ...

    async def record_attempt(self, message_id: str) -> int:
        """Count a run started for a conversation mailbox message; return its attempts so far."""
        ...

    async def mark_handled(self, message_id: str) -> None:
        """Record that the message's run completed and its reply was sent."""
        ...

    async def get_handled_message(self, message_id: str) -> HandledMessage | None: ...
