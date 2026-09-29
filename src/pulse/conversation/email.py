"""The email channel: polling the conversation mailbox and replying in a thread."""

import logging

from pulse.config import Config, is_reviewer
from pulse.conversation.runner import ConversationRunner, TurnResult
from pulse.errors import ConversationError
from pulse.mail import Mailbox
from pulse.models import Message, ReviewerMessage
from pulse.pipeline.render import render_notice
from pulse.pipeline.rules import is_automatic_reply
from pulse.store import EditionStore

log = logging.getLogger("pulse.conversation.email")

# The chat agent's exact reply when a message needs no response, such as reviewers replying to
# each other; the message is still recorded as feedback, but nothing is sent back.
NO_REPLY = "NO_REPLY"


async def _handle(
    config: Config, conversation: Mailbox, runner: ConversationRunner, msg: Message
) -> None:
    async def deliver(result: TurnResult) -> None:
        # A new version's reviewer email is sent only as this reply, so it goes even when the
        # agent has nothing to say.
        if result.version_email is not None:
            html = result.version_email.html
        elif result.reply != NO_REPLY:
            html = render_notice("Techary Pulse", result.reply)
        else:
            return
        await conversation.reply(msg.id, config.reviewers, html)

    message = ReviewerMessage(
        reviewer=msg.sender_address,
        reviewer_name=msg.sender_name,
        channel="email",
        text=msg.body,
        received_at=msg.received_at,
    )
    await runner.run_turn(message, deliver, handled_message_id=msg.id)


async def poll_once(
    config: Config, conversation: Mailbox, store: EditionStore, runner: ConversationRunner
) -> None:
    """Poll the conversation mailbox once, running one chat agent turn per new message.

    Messages are handled in received order. A message already handled (recovering from a crash
    between saving and moving) is moved straight to `Processed`. An automatic reply or a message
    from a non-reviewer is moved to `Rejected` without an agent run. Otherwise the chat agent
    runs and replies, and the message moves to `Processed`. On failure the message stays in the
    inbox for the next poll, up to `chat.max_attempts`, after which it moves to `Rejected`.
    """
    for msg in sorted(await conversation.list_inbox(), key=lambda m: m.received_at):
        if await store.is_handled(msg.id):
            await conversation.move(msg.id, config.mailboxes.processed_folder)
            continue
        if is_automatic_reply(msg) or not is_reviewer(config, msg.sender_address):
            await conversation.move(msg.id, config.mailboxes.rejected_folder)
            continue

        attempts = await store.record_attempt(msg.id)
        try:
            await _handle(config, conversation, runner, msg)
        except ConversationError as exc:
            log.warning(
                "chat turn failed",
                extra={
                    "message_id": msg.id,
                    "attempts": attempts,
                    "error_type": type(exc).__name__,
                },
            )
            if attempts >= config.chat.max_attempts:
                await conversation.move(msg.id, config.mailboxes.rejected_folder)
            continue
        await conversation.move(msg.id, config.mailboxes.processed_folder)
