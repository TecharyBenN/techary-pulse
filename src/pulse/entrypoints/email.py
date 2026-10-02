"""The email channel: EmailChannel answers reviewer emails in the conversation mailbox."""

import functools
import logging
from collections.abc import Awaitable, Callable

from pulse.agents.orchestrator.run import Orchestrator, RunReply
from pulse.entities.content import Content, Version
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import PulseError
from pulse.entities.lifecycle import Newsletter
from pulse.entities.mail import Body, InboundEmail, Mailbox, channel_rejection
from pulse.entities.store import Store
from pulse.services.outbox import Outbox

_log = logging.getLogger(__name__)


class EmailChannel:
    """Passes each reviewer email to the orchestrator and sends its reply in the newsletter's
    thread."""

    def __init__(
        self,
        conversation_mailbox: Mailbox,
        orchestrator: Orchestrator,
        store: Store,
        outbox: Outbox,
        reviewer_email: Callable[[Newsletter, Version, str | None], Awaitable[Body]],
        render_newsletter: Callable[[Content, str | None], str],
        render_reply: Callable[[str], str],
        *,
        max_attempts: int,
        processed_folder: str,
        rejected_folder: str,
    ) -> None:
        """`reviewer_email` and the render functions take the orchestrator's reply in Markdown,
        which they show before any newsletter."""
        self._mailbox = conversation_mailbox
        self._orchestrator = orchestrator
        self._store = store
        self._outbox = outbox
        self._reviewer_email = reviewer_email
        self._render_newsletter = render_newsletter
        self._render_reply = render_reply
        self._max_attempts = max_attempts
        self._processed = processed_folder
        self._rejected = rejected_folder

    async def poll(self) -> None:
        """Handle each inbox message in the order it arrived, stopping at one whose run failed,
        so it is retried at the next poll before any later message."""
        for email in await self._mailbox.list_inbox():
            if not await self._handle(email):
                return

    async def _handle(self, email: InboundEmail) -> bool:
        """Handle one message; return False when it stays in the inbox to be retried."""
        message_id = email.message_id
        seen = await self._store.get_handled_message(message_id)
        if seen is not None and seen.handled:
            await self._move(email, self._processed, "processed")
            return True
        if reason := channel_rejection(email.headers):
            await self._move(email, self._rejected, reason)
            return True
        if seen is not None and seen.attempts >= self._max_attempts:
            # Earlier runs were interrupted before they could fail, as a crash would.
            await self._give_up(email, seen.attempts)
            return True
        attempts = await self._store.record_attempt(message_id)
        message = ReviewerMessage(
            message_id=message_id,
            # Exchange authenticated the sender, and accepts mail only from the reviewers list.
            author=email.sender_address,
            channel="email",
            text=email.body,
            received=email.received,
        )
        try:
            await self._orchestrator.handle(message, on_reply=functools.partial(self._reply, email))
        except PulseError:
            _log.exception("email_failed", extra={"message_id": message_id, "attempts": attempts})
            if attempts < self._max_attempts:
                return False
            await self._give_up(email, attempts)
            return True
        await self._store.mark_handled(message_id)
        await self._move(email, self._processed, "processed")
        return True

    async def _reply(self, email: InboundEmail, reply: RunReply) -> None:
        """Send the run's reply while the run lock is held, so no other run changes the
        newsletter's thread meanwhile."""
        if reply.text is None and reply.newsletter is None:
            return
        newsletter = await self._store.get_latest_newsletter()
        body = await self._body(reply, newsletter)
        await self._outbox.reply(
            newsletter, email.message_id, body, presents_version=reply.version is not None
        )

    async def _body(self, reply: RunReply, newsletter: Newsletter | None) -> Body:
        """The reply, then the presented version's reviewer email or the newsletter shown."""
        if reply.version is not None and newsletter is not None:
            return await self._reviewer_email(newsletter, reply.version, reply.text)
        if reply.newsletter is not None:
            html = self._render_newsletter(reply.newsletter, reply.text)
        else:
            html = self._render_reply(reply.text or "")
        return Body(content=html, content_type="html")

    async def _give_up(self, email: InboundEmail, attempts: int) -> None:
        await self._move(email, self._rejected, "max_attempts")
        await self._outbox.alert(
            "reviewer message not handled",
            f"Conversation mailbox message {email.message_id} failed {attempts} times and was"
            f" moved to {self._rejected}.",
        )

    async def _move(self, email: InboundEmail, folder: str, outcome: str) -> None:
        await self._mailbox.move(email.message_id, folder)
        _log.info("email_message", extra={"message_id": email.message_id, "outcome": outcome})
