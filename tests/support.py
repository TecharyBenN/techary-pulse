"""Test stand-ins: the fake mailbox, scripted models and message builders."""

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.models import Message, OutgoingEmail


class MailboxFailure(Exception):
    """Raised by the fake to simulate a mailbox failure."""


class FakeMailbox:
    def __init__(self, messages: list[Message]) -> None:
        self.inbox: dict[str, Message] = {message.id: message for message in messages}
        self.folders: dict[str, list[str]] = {}
        self.sent: list[OutgoingEmail] = []
        self.fail_send = False
        self.fail_move_after: int | None = None
        self._moves = 0

    def list_inbox(self) -> list[Message]:
        return list(self.inbox.values())

    def move(self, message_id: str, folder: str) -> None:
        if self.fail_move_after is not None and self._moves >= self.fail_move_after:
            raise MailboxFailure("move failed")
        # Like Graph with immutable IDs, a message already moved can be moved again.
        self.inbox.pop(message_id, None)
        for ids in self.folders.values():
            if message_id in ids:
                ids.remove(message_id)
        self.folders.setdefault(folder, []).append(message_id)
        self._moves += 1

    def send(self, email: OutgoingEmail) -> None:
        if self.fail_send:
            raise MailboxFailure("send failed")
        self.sent.append(email)


def scripted(responses: Iterable[Any]) -> FunctionModel:
    """A model that returns each response in turn, as JSON unless it is already a string."""
    remaining = iter(responses)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = next(remaining)
        text = response if isinstance(response, str) else json.dumps(response)
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(respond)


def answering(fn: Callable[[str], Any]) -> FunctionModel:
    """A model whose response is computed from the last user message."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = next(
            part.content
            for message in reversed(messages)
            for part in getattr(message, "parts", [])
            if getattr(part, "part_kind", "") == "user-prompt"
        )
        response = fn(str(prompt))
        return ModelResponse(parts=[TextPart(json.dumps(response))])

    return FunctionModel(respond)


def message(
    id: str = "m1",
    sender_name: str = "Priya Shah",
    sender_address: str = "priya.shah@techary.ai",
    subject: str = "Signed Northwind Retail",
    body: str = "Tom Evans and I signed Northwind Retail on 22 September.",
    headers: dict[str, str] | None = None,
    has_attachments: bool = False,
) -> Message:
    return Message(
        id=id,
        sender_name=sender_name,
        sender_address=sender_address,
        subject=subject,
        received_at=datetime(2026, 9, 22, 9, tzinfo=UTC),
        body=body,
        headers=headers or {},
        has_attachments=has_attachments,
    )
