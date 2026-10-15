"""Reviewer messages, the start instruction, and the email channel's record of the messages it
has handled."""

import itertools
import uuid
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import AwareDatetime

from pulse.entities.base import Entity

Channel = Literal["email", "librechat"]
# What posted a start instruction.
StartSource = Literal["scheduler", "pulse draft"]

# Tags a start instruction's saved steps, in place of a reviewer message ID, with its source.
_START_TAGS: dict[StartSource, str] = {
    "scheduler": "start-scheduler-",
    "pulse draft": "start-draft-",
}


class ReviewerMessage(Entity):
    """One message from a verified reviewer, through either channel."""

    message_id: str
    # The reviewer's Entra object ID, as the channel verified it.
    author: str
    channel: Channel
    text: str
    received: AwareDatetime


class StartInstruction(Entity):
    """The fixed message, posted by the scheduler and by `pulse draft`, that asks the
    orchestrator to draft a newsletter. It has no reviewer, so it is never feedback."""

    message_id: str
    source: StartSource

    @classmethod
    def new(cls, source: StartSource) -> Self:
        return cls(message_id=f"{_START_TAGS[source]}{uuid.uuid4()}", source=source)


# What an orchestrator run answers.
RunMessage = ReviewerMessage | StartInstruction


def start_instruction(message_id: str) -> StartInstruction | None:
    """The start instruction whose run saved steps under `message_id`, if it was one."""
    return next(
        (
            StartInstruction(message_id=message_id, source=source)
            for source, tag in _START_TAGS.items()
            if message_id.startswith(tag)
        ),
        None,
    )


def channel_changed(feedback: Sequence[ReviewerMessage], message: ReviewerMessage) -> bool:
    """Whether the conversation's previous message came through the other channel, so the
    reviewer's screen shows none of what happened since.

    `feedback` is the newsletter's recorded messages, in order; only those before `message`
    count, because a retried message is already recorded.
    """
    earlier = list(itertools.takewhile(lambda m: m.message_id != message.message_id, feedback))
    return bool(earlier) and earlier[-1].channel != message.channel


class HandledMessage(Entity):
    """A conversation mailbox message the email channel has seen."""

    message_id: str
    # Runs started for the message, including any still running or interrupted.
    attempts: int
    # Whether its run completed and its reply was sent, so it needs only moving.
    handled: bool
