"""Reviewer messages: the input to every orchestrator run, and the feedback it records."""

from typing import Literal

from pydantic import AwareDatetime

from pulse.entities.base import Entity

Channel = Literal["email", "librechat"]


class ReviewerMessage(Entity):
    message_id: str
    # The reviewer's address, as the channel identified it.
    author: str
    channel: Channel
    text: str
    received: AwareDatetime
