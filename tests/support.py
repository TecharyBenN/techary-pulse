"""Test stand-ins: the fake mailbox, scripted models, message builders and a seeded edition."""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.errors import GraphError
from pulse.models import Draft, Edition, Message, OutgoingEmail, Version
from pulse.store import EditionStore, ItemRow, SourceRow

REVIEWER = "reviewer@techary.ai"
JUDGE_PASS = {"intro": {"supported": True, "reason": ""}, "entries": []}


class MailboxFailure(GraphError):
    """Raised by the fake to simulate a mailbox failure, as GraphMailbox raises GraphError."""


@dataclass(frozen=True)
class Reply:
    message_id: str
    to: list[str]
    html: str


class FakeMailbox:
    def __init__(self, messages: list[Message]) -> None:
        self.inbox: dict[str, Message] = {message.id: message for message in messages}
        self.folders: dict[str, list[str]] = {}
        self.sent: list[OutgoingEmail] = []
        self.replies: list[Reply] = []
        self.fail_send = False
        self.fail_move_after: int | None = None
        self._moves = 0

    async def list_inbox(self) -> list[Message]:
        return list(self.inbox.values())

    async def move(self, message_id: str, folder: str) -> None:
        if self.fail_move_after is not None and self._moves >= self.fail_move_after:
            raise MailboxFailure("move failed")
        # Like Graph with immutable IDs, a message already moved can be moved again.
        self.inbox.pop(message_id, None)
        for ids in self.folders.values():
            if message_id in ids:
                ids.remove(message_id)
        self.folders.setdefault(folder, []).append(message_id)
        self._moves += 1

    async def send(self, email: OutgoingEmail) -> None:
        if self.fail_send:
            raise MailboxFailure("send failed")
        self.sent.append(email)

    async def reply(self, message_id: str, to: list[str], html: str) -> None:
        if self.fail_send:
            raise MailboxFailure("send failed")
        self.replies.append(Reply(message_id, to, html))


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


def calling(*steps: tuple[str, dict[str, Any]] | str) -> FunctionModel:
    """A model that makes each tool call in `steps` in order, then returns the last as text.

    Each step is either `(tool_name, args)`, to call a tool, or a plain string, the final reply.
    """
    remaining = list(steps)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        step = remaining.pop(0)
        if isinstance(step, str):
            return ModelResponse(parts=[TextPart(step)])
        name, args = step
        call_id = f"call-{len(messages)}-{name}"
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=call_id)])

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
        self.replies: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.injected: list[httpx.Response] = []
        self._reply_drafts: dict[str, dict[str, Any]] = {}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

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
        if request.method == "POST" and path.endswith("/createReplyAll"):
            message_id = path.split("/")[-2]
            reply_id = f"reply-{message_id}"
            self._reply_drafts[reply_id] = {"source_message_id": message_id}
            return httpx.Response(201, json={"id": reply_id})
        if request.method == "PATCH" and path.split("/")[-1] in self._reply_drafts:
            reply_id = path.split("/")[-1]
            self._reply_drafts[reply_id].update(json.loads(request.content))
            return httpx.Response(200, json={"id": reply_id})
        if request.method == "POST" and path.endswith("/send") and not path.endswith("/sendMail"):
            reply_id = path.split("/")[-2]
            self.replies.append(self._reply_drafts[reply_id])
            return httpx.Response(202)
        return httpx.Response(404, json={"error": {"code": "NotFound"}})


def at(day: int, hour: int = 9, minute: int = 0) -> datetime:
    """A UTC time in September 2026, the month the tests are set in."""
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


# A seeded edition: one included item from m01 and one record from m02 excluded for sensitivity.
ITEM_ROWS = [
    ItemRow(
        item_id="item-1",
        kind="item",
        record={
            "item_id": "item-1",
            "category": "customer_win",
            "facts": ["Signed Northwind Retail"],
            "people": ["Priya Shah"],
            "source_message_ids": ["m01"],
            "sender_names": ["Priya Shah"],
            "received_dates": [at(20).isoformat()],
        },
        source_message_ids=["m01"],
    ),
    ItemRow(
        item_id="excluded-1",
        kind="excluded",
        record={
            "message_id": "m02",
            "is_update": True,
            "exclusion_reason": None,
            "category": "team_news",
            "summary": "A promotion.",
            "facts": ["Ben Carter was promoted"],
            "people": ["Ben Carter"],
            "sensitivity": [{"type": "personal", "evidence": "mentions a promotion"}],
            "reason": "sensitivity",
        },
        source_message_ids=["m02"],
    ),
]
SOURCE_ROWS = [
    SourceRow(
        message_id="m01",
        outcome="included",
        subject="Signed Northwind Retail",
        sender_name="Priya Shah",
        sender_address="priya.shah@techary.ai",
        received_at=at(20),
        body="I signed Northwind Retail.",
    ),
    SourceRow(
        message_id="m02",
        outcome="excluded",
        subject="Ben's promotion",
        sender_name="Ben Carter",
        sender_address="ben.carter@techary.ai",
        received_at=at(21),
        body="I was promoted to senior service desk analyst.",
    ),
]
DRAFT_V1 = Draft.model_validate(
    {
        "intro": "A good week.",
        "sections": [
            {
                "category": "customer_win",
                "entries": [
                    {
                        "item_id": "item-1",
                        "text": "Priya Shah signed Northwind Retail.",
                        "people": ["Priya Shah"],
                    }
                ],
            }
        ],
    }
)


async def seed_edition(
    store: EditionStore, created_at: datetime | None = None, draft: Draft = DRAFT_V1
) -> str:
    """Create the seeded edition, at version 1, and return its ID."""
    version = Version(
        number=1,
        draft=draft,
        headline="A strong week",
        item_ids=["item-1"],
        check_results=[],
        creator="pulse",
        created_at=created_at or at(22),
    )
    edition = await store.create_edition(
        build_id="b1", trigger="command", items=ITEM_ROWS, sources=SOURCE_ROWS, version=version
    )
    return edition.id


async def approve_seeded(store: EditionStore, **changes: Any) -> Edition:
    """Mark the open edition approved at version 1 by REVIEWER, with any further changes."""
    edition = await store.open_edition()
    assert edition is not None
    approved = edition.model_copy(
        update={"state": "approved", "approved_version": 1, "approver": REVIEWER, **changes}
    )
    await store.update_edition(approved)
    return approved
