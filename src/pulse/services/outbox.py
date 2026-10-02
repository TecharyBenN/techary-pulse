"""Outbox: the emails Pulse sends from the conversation mailbox to the reviewers and operators."""

import logging
from collections.abc import Callable, Sequence
from zoneinfo import ZoneInfo

from pulse.entities.errors import MailboxError
from pulse.entities.lifecycle import Newsletter
from pulse.entities.mail import Body, Mailbox, MessageId, OutboundEmail, subject
from pulse.entities.store import Store

# The subject prefix of the email that starts a newsletter's thread with its first version.
DRAFT_PREFIX = "Draft:"

_log = logging.getLogger(__name__)


class Outbox:
    """Sends reviewer emails in the newsletter's email thread, notices and operator alerts."""

    def __init__(
        self,
        conversation_mailbox: Mailbox,
        store: Store,
        render_notice: Callable[[str, str], str],
        *,
        reviewers: str,
        operator_alerts: Sequence[str],
        subject_template: str,
        timezone: ZoneInfo,
    ) -> None:
        self._mailbox = conversation_mailbox
        self._store = store
        self._render_notice = render_notice
        self._reviewers = reviewers
        self._operator_alerts = list(operator_alerts)
        self._subject_template = subject_template
        self._timezone = timezone

    async def to_reviewers(
        self, newsletter: Newsletter, body: Body, prefix: str = DRAFT_PREFIX
    ) -> MessageId:
        """Reply to the latest message in the newsletter's email thread, or start the thread
        with the subject after `prefix`."""
        if newsletter.thread_message_id is not None:
            return await self._mailbox.reply(newsletter.thread_message_id, [self._reviewers], body)
        email = OutboundEmail(
            to=[self._reviewers],
            subject=subject(
                self._subject_template, newsletter.opened_at, self._timezone, prefix=prefix
            ),
            body=body,
            reply_to=None,
        )
        return await self._mailbox.send(email)

    async def thread(
        self, newsletter: Newsletter, body: Body, prefix: str = DRAFT_PREFIX
    ) -> Newsletter:
        """Email the reviewers in the newsletter's thread, as `to_reviewers` does, and save the
        email as the thread's latest message. Return the newsletter as saved."""
        sent_id = await self.to_reviewers(newsletter, body, prefix)
        threaded = newsletter.model_copy(update={"thread_message_id": sent_id})
        await self._store.save_newsletter(threaded)
        return threaded

    async def reply(
        self,
        newsletter: Newsletter | None,
        message_id: MessageId,
        body: Body,
        presents_version: bool,
    ) -> None:
        """Answer a reviewer's email in the newsletter's thread. Before the thread exists, only a
        presented version starts it; any other reply goes to the reviewer's own message."""
        if newsletter is not None and (
            newsletter.thread_message_id is not None or presents_version
        ):
            await self.thread(newsletter, body)
        else:
            await self._mailbox.reply(message_id, [self._reviewers], body)

    async def notice(
        self, newsletter: Newsletter, title: str, text: str
    ) -> tuple[Newsletter, bool]:
        """Email the reviewers a notice starting with `title`, and save it as the thread's latest
        message. Return the newsletter as saved, and whether the notice was sent: a notice that
        cannot be sent is logged, because the change it reports is already saved."""
        body = Body(content=self._render_notice(title, text), content_type="html")
        try:
            return await self.thread(newsletter, body, f"{title}:"), True
        except MailboxError:
            _log.exception("notice_failed", extra={"conversation_id": newsletter.newsletter_id})
            return newsletter, False

    async def alert(self, summary: str, text: str) -> None:
        """Email the operators; an alert that cannot be sent is logged."""
        email = OutboundEmail(
            to=self._operator_alerts,
            subject=f"Pulse alert: {summary}",
            body=Body(content=text, content_type="text"),
            reply_to=None,
        )
        try:
            await self._mailbox.send(email)
        except MailboxError:
            _log.exception("alert_failed")
