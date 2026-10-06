"""Delivery: sends an approved newsletter to all staff and moves its screened emails."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from pulse.entities.content import Content, EntryCredits
from pulse.entities.errors import Refusal, StoreError
from pulse.entities.lifecycle import Newsletter, is_due, mark_sent, start_send
from pulse.entities.mail import (
    Body,
    Destination,
    Mailbox,
    OutboundEmail,
    destination,
    display_date,
    subject,
)
from pulse.entities.store import DELIVERY_NOTE, Store
from pulse.services.outbox import Outbox

_log = logging.getLogger(__name__)


class Delivery:
    """Sends the open newsletter when it is due. The only code that sends to the all-staff list."""

    def __init__(
        self,
        store: Store,
        conversation_mailbox: Mailbox,
        submissions_mailbox: Mailbox,
        outbox: Outbox,
        render_newsletter: Callable[[Content, EntryCredits], str],
        history_note: Callable[[str], bytes],
        clock: Callable[[], datetime],
        lock: asyncio.Lock,
        *,
        all_staff: str,
        reply_to: str,
        subject_template: str,
        timezone: ZoneInfo,
        processed_folder: str,
        rejected_folder: str,
    ) -> None:
        """`lock` is the run lock, so no run changes the newsletter while it is sent."""
        self._store = store
        self._conversation = conversation_mailbox
        self._submissions = submissions_mailbox
        self._outbox = outbox
        self._render_newsletter = render_newsletter
        self._history_note = history_note
        self._clock = clock
        self._lock = lock
        self._all_staff = all_staff
        self._reply_to = reply_to
        self._subject_template = subject_template
        self._timezone = timezone
        self._folders: dict[Destination, str] = {
            "processed": processed_folder,
            "rejected": rejected_folder,
        }

    async def deliver(self) -> None:
        """Send the open newsletter if it is approved and its send time has come."""
        async with self._lock:
            newsletter = await self._store.get_open_newsletter()
            if newsletter is None or not is_due(newsletter, self._clock()):
                return
            try:
                started = start_send(newsletter)
            except Refusal as refusal:
                await self._outbox.alert(
                    "newsletter not sent",
                    f"Newsletter {newsletter.newsletter_id} was not sent: {refusal}.",
                )
                _log.info("delivery", extra=_fields(newsletter, "recheck_failed"))
                return
            await self._store.save_newsletter(started)
            await self._send(started)
            _log.info("delivery", extra=_fields(started, "sent"))

    async def _send(self, started: Newsletter) -> None:
        newsletter_id = started.newsletter_id
        version_number = started.approved_version
        version = (
            await self._store.get_version(newsletter_id, version_number) if version_number else None
        )
        if version is None:
            raise StoreError(f"approved version {version_number} is not stored")
        email = OutboundEmail(
            to=[self._all_staff],
            subject=subject(self._subject_template, started.opened_at, self._timezone),
            # Credit lines, but no flags: those are for the reviewers.
            body=Body(
                content=self._render_newsletter(version.content, version.notes.credits),
                content_type="html",
            ),
            # Staff replies arrive as pending emails for the next newsletter.
            reply_to=self._reply_to,
        )
        await self._conversation.send(email)
        now = self._clock()
        sent = mark_sent(started, now)
        await self._store.save_newsletter(sent)
        local = now.astimezone(self._timezone)
        note = (
            f"Pulse sent version {version_number} of this newsletter to all staff on "
            f"{display_date(now, self._timezone)} at {local:%H:%M}."
        )
        await self._store.append_history(newsletter_id, DELIVERY_NOTE, self._history_note(note))
        text = f"Version {version_number} was sent to all staff."
        await self._outbox.notice(sent, "Sent", text)
        await self._move(newsletter_id)

    async def _move(self, newsletter_id: str) -> None:
        """Move each screened email to its folder, recording each move as it happens."""
        records = await self._store.list_extract_records(newsletter_id)
        extracted = {record.message_id for record in records}
        for email in await self._store.list_screened_emails(newsletter_id):
            folder = destination(email, email.message_id in extracted)
            if folder is None or email.moved:
                continue
            await self._submissions.move(email.message_id, self._folders[folder])
            await self._store.mark_moved(newsletter_id, email.message_id)


def _fields(newsletter: Newsletter, outcome: str) -> dict[str, object]:
    return {"conversation_id": newsletter.newsletter_id, "outcome": outcome}
