"""A fake Microsoft Graph for one mailbox, served through httpx.MockTransport."""

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote

import httpx
from azure.core.credentials import AccessToken
from msgraph.graph_service_client import GraphServiceClient

from pulse.adapters.graph import graph_client
from pulse.entities.mail import InboundEmail

MAILBOX = "pulse@example.org"
PAGE_SIZE = 2


def graph_message(email: InboundEmail) -> dict[str, Any]:
    """The message as Graph lists it."""
    return {
        "id": email.message_id,
        "from": {"emailAddress": {"name": email.sender_name, "address": email.sender_address}},
        "subject": email.subject,
        "receivedDateTime": email.received.isoformat().replace("+00:00", "Z"),
        "uniqueBody": {"contentType": "text", "content": email.body},
        "internetMessageHeaders": [{"name": k, "value": v} for k, v in email.headers.items()],
        "hasAttachments": email.has_attachments,
    }


class _Credential:
    """Gives every request the same access token."""

    async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        return AccessToken("token-1", 2**31)

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> _Credential:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class FakeGraph:
    """Holds one mailbox's inbox and folders, and records every request."""

    def __init__(self, inbox: Iterable[InboundEmail] = ()) -> None:
        self.inbox = [graph_message(email) for email in inbox]
        self.folders: dict[str, str] = {}
        self.moved: dict[str, list[str]] = {}
        self.sent: list[dict[str, Any]] = []
        self.sent_ids: list[str] = []
        self.drafts: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        # Responses to answer with, or errors to raise, in order, before handling requests.
        self.failures: list[httpx.Response | httpx.HTTPError] = []

    def client(self) -> GraphServiceClient:
        """A Graph SDK client, with its standard middleware, that sends requests here."""
        transport = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return graph_client(_Credential(), transport)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, httpx.HTTPError):
                raise failure
            return failure
        path = unquote(request.url.path.removeprefix("/v1.0"))
        prefix = f"/users/{MAILBOX}"
        if not path.startswith(prefix):
            return httpx.Response(404)
        return self._route(request, path.removeprefix(prefix))

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        method = request.method
        if method == "GET" and path == "/mailFolders/inbox/messages":
            return self._page(request)
        if method == "GET" and path == "/mailFolders":
            wanted = re.fullmatch(r"displayName eq '(.*)'", request.url.params["$filter"])
            assert wanted is not None
            name = wanted.group(1).replace("''", "'")
            found = [{"id": self.folders[name]}] if name in self.folders else []
            return httpx.Response(200, json={"value": found})
        if method == "POST" and path == "/mailFolders":
            folder_id = f"folder-{len(self.folders) + 1}"
            self.folders[body["displayName"]] = folder_id
            return httpx.Response(201, json={"id": folder_id})
        if method == "POST" and path == "/messages":
            draft_id = self._new_id("draft")
            self.drafts[draft_id] = body
            return httpx.Response(201, json={"id": draft_id})
        if match := re.fullmatch(r"/messages/([^/]+)/(move|createReplyAll|send)", path):
            # The SDK writes the move's destinationId as DestinationId; Graph ignores the case.
            body = {key[0].lower() + key[1:]: value for key, value in body.items()}
            return self._message_action(match.group(1), match.group(2), body)
        if method == "PATCH" and (match := re.fullmatch(r"/messages/([^/]+)", path)):
            self.drafts[match.group(1)] |= body
            return httpx.Response(200, json={"id": match.group(1)})
        return httpx.Response(404)

    def _message_action(self, message_id: str, action: str, body: dict[str, Any]) -> httpx.Response:
        if action == "send":
            self.sent.append(self.drafts.pop(message_id))
            self.sent_ids.append(message_id)
            return httpx.Response(202)
        known = [message["id"] for message in self.inbox] + self.sent_ids
        if message_id not in known:
            return httpx.Response(404)
        if action == "createReplyAll":
            reply_id = self._new_id("reply")
            self.drafts[reply_id] = {"replyTo": message_id, "ccRecipients": [{"x": 1}]}
            return httpx.Response(201, json={"id": reply_id})
        folder = next(name for name, id_ in self.folders.items() if id_ == body["destinationId"])
        self.inbox = [message for message in self.inbox if message["id"] != message_id]
        self.moved.setdefault(folder, []).append(message_id)
        return httpx.Response(201, json={"id": message_id})

    def _new_id(self, kind: str) -> str:
        """Numbered across drafts and sent messages, so every message gets its own ID."""
        return f"{kind}-{len(self.drafts) + len(self.sent) + 1}"

    def _page(self, request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params.get("$skip", "0"))
        page = self.inbox[skip : skip + PAGE_SIZE]
        body: dict[str, Any] = {"value": page}
        if skip + PAGE_SIZE < len(self.inbox):
            body["@odata.nextLink"] = str(request.url.copy_set_param("$skip", skip + PAGE_SIZE))
        return httpx.Response(200, json=body)
