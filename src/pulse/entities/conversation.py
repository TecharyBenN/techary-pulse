"""Reviewer messages, and the email channel's record of the messages it has handled."""

import itertools
from collections.abc import Sequence
from typing import Literal

from pydantic import AwareDatetime

from pulse.entities.base import Entity

Channel = Literal["email", "librechat"]


class ReviewerMessage(Entity):
    """One message from a verified reviewer, through either channel."""

    message_id: str
    # The reviewer's Entra object ID, as the channel verified it.
    author: str
    channel: Channel
    text: str
    received: AwareDatetime


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
