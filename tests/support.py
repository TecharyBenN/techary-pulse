"""Test stand-ins: the fake mailbox, scripted models and message builders."""

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
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


class FakeGraph:
    """Answers the Graph requests GraphMailbox makes, from an in-memory mailbox.

    Queue responses in ``injected`` to simulate throttling or errors before the real answer.
    """

    PAGE = 2

    def __init__(self, messages: list[Message]) -> None:
        self.inbox: list[Message] = list(messages)
        self.folders: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.injected: list[httpx.Response] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    @staticmethod
    def raw(message: Message) -> dict[str, Any]:
        return {
            "id": message.id,
            "from": {
                "emailAddress": {"name": message.sender_name, "address": message.sender_address}
            },
            "subject": message.subject,
            "receivedDateTime": message.received_at.isoformat().replace("+00:00", "Z"),
            "uniqueBody": {"contentType": "text", "content": message.body},
            "internetMessageHeaders": [{"name": k, "value": v} for k, v in message.headers.items()],
            "hasAttachments": message.has_attachments,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.injected:
            return self.injected.pop(0)
        path = request.url.path
        if request.method == "GET" and path.endswith("/mailFolders/inbox/messages"):
            skip = int(request.url.params.get("skip", "0"))
            page = self.inbox[skip : skip + self.PAGE]
            body: dict[str, Any] = {"value": [self.raw(m) for m in page]}
            if skip + self.PAGE < len(self.inbox):
                body["@odata.nextLink"] = str(request.url.copy_set_param("skip", skip + self.PAGE))
            return httpx.Response(200, json=body)
        if request.method == "GET" and path.endswith("/mailFolders"):
            name = request.url.params["$filter"].split("'")[1]
            found = [{"id": f["id"]} for n, f in self.folders.items() if n == name]
            return httpx.Response(200, json={"value": found})
        if request.method == "POST" and path.endswith("/mailFolders"):
            name = json.loads(request.content)["displayName"]
            self.folders[name] = {"id": f"folder-{name}", "messages": []}
            return httpx.Response(201, json={"id": f"folder-{name}"})
        if request.method == "POST" and path.endswith("/move"):
            message_id = path.split("/")[-2]
            destination = json.loads(request.content)["destinationId"]
            self.inbox = [m for m in self.inbox if m.id != message_id]
            for folder in self.folders.values():
                if message_id in folder["messages"]:
                    folder["messages"].remove(message_id)
                if folder["id"] == destination:
                    folder["messages"].append(message_id)
            return httpx.Response(201, json={"id": message_id})
        if request.method == "POST" and path.endswith("/sendMail"):
            self.sent.append(json.loads(request.content)["message"])
            return httpx.Response(202)
        return httpx.Response(404, json={"error": {"code": "NotFound"}})
