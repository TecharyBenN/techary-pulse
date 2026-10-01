"""Pulse's own emails from the conversation mailbox: to the reviewers in the newsletter's email
thread, and alerts to the operators."""

import logging
from collections.abc import Callable, Sequence
from zoneinfo import ZoneInfo

from pulse.entities.errors import MailboxError
from pulse.entities.lifecycle import Newsletter
from pulse.entities.mail import Body, Mailbox, MessageId, OutboundEmail, subject
from pulse.entities.store import Store

_log = logging.getLogger(__name__)


class Outbox:
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

    async def to_reviewers(self, newsletter: Newsletter, body: Body, prefix: str) -> MessageId:
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

    async def notice(
        self, newsletter: Newsletter, title: str, text: str
    ) -> tuple[Newsletter, bool]:
        """Email the reviewers a notice starting with `title`, and save it as the thread's latest
        message. Return the newsletter as saved, and whether the notice was sent: a notice that
        cannot be sent is logged, because the change it reports is already saved."""
        body = Body(content=self._render_notice(title, text), content_type="html")
        try:
            notice_id = await self.to_reviewers(newsletter, body, f"{title}:")
        except MailboxError as error:
            _log.error(
                "notice_failed",
                extra={
                    "conversation_id": newsletter.newsletter_id,
                    "error_type": type(error).__name__,
                },
            )
            return newsletter, False
        threaded = newsletter.model_copy(update={"thread_message_id": notice_id})
        await self._store.save_newsletter(threaded)
        return threaded, True

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
        except MailboxError as error:
            _log.error("alert_failed", extra={"error_type": type(error).__name__})
