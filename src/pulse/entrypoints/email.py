"""The email channel: reviewer messages in the conversation mailbox, each passed to the
orchestrator, with its reply in the newsletter's email thread."""

import functools
import logging
from collections.abc import Awaitable, Callable

from pulse.agents.orchestrator.run import Orchestrator, RunReply
from pulse.entities.content import Content, Version
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import PulseError
from pulse.entities.lifecycle import Newsletter
from pulse.entities.mail import Body, InboundEmail, Mailbox, channel_rejection
from pulse.entities.review import Review
from pulse.entities.store import Store
from pulse.services.mail import Outbox
from pulse.services.operations import DRAFT_PREFIX

_log = logging.getLogger(__name__)


class EmailChannel:
    def __init__(
        self,
        conversation_mailbox: Mailbox,
        orchestrator: Orchestrator,
        store: Store,
        outbox: Outbox,
        review: Callable[[str, Version], Awaitable[Review]],
        render_reviewer_email: Callable[[Version, Review, str | None], str],
        render_newsletter: Callable[[Content, str | None], str],
        render_reply: Callable[[str], str],
        *,
        max_attempts: int,
        processed_folder: str,
        rejected_folder: str,
    ) -> None:
        """`review` builds a version's review section; the render functions take the
        orchestrator's reply in Markdown, which they show before any newsletter."""
        self._mailbox = conversation_mailbox
        self._orchestrator = orchestrator
        self._store = store
        self._outbox = outbox
        self._review = review
        self._render_reviewer_email = render_reviewer_email
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
            # Its run completed and its reply was sent, so it needs only moving.
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
        except PulseError as error:
            fields = {"message_id": message_id, "attempts": attempts}
            _log.info("email_failed", extra=fields | {"error_type": type(error).__name__})
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
        body = Body(content=await self._body(reply, newsletter), content_type="html")
        # Only a presented version starts a thread; any other reply before one exists goes to
        # the reviewer's own message.
        if newsletter is not None and (
            newsletter.thread_message_id is not None or reply.version is not None
        ):
            await self._outbox.thread(newsletter, body, DRAFT_PREFIX)
        else:
            await self._outbox.reply_to_message(email.message_id, body)

    async def _body(self, reply: RunReply, newsletter: Newsletter | None) -> str:
        """The reply, then the presented version's reviewer email or the newsletter shown."""
        if reply.version is not None and newsletter is not None:
            review = await self._review(newsletter.newsletter_id, reply.version)
            return self._render_reviewer_email(reply.version, review, reply.text)
        if reply.newsletter is not None:
            return self._render_newsletter(reply.newsletter, reply.text)
        return self._render_reply(reply.text or "")

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
