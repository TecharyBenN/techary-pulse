"""Connections to the dev tenant for `live` tests, using ./config.yaml and the certificate it
names."""

import asyncio
import base64
import os
from typing import Any
from urllib.parse import quote

import httpx

from pulse.adapters.clock import SystemClock
from pulse.adapters.graph import CertificateCredential, GraphMailbox
from pulse.agents.orchestrator.run import Orchestrator
from pulse.config import Config
from pulse.entities.clock import Clock
from pulse.entities.mail import Mailbox
from pulse.entities.store import Store
from pulse.main import build_operations, build_orchestrator, build_outbox
from tests.emails import STAFF_DOMAIN

# PR_MESSAGE_FLAGS set to read, so a created message is received mail, not an unsent draft.
_RECEIVED = [{"id": "Integer 0x0E07", "value": "1"}]


def graph_mailbox(config: Config, client: httpx.AsyncClient, address: str) -> GraphMailbox:
    return GraphMailbox(client, _credential(config).token, address, config.graph.max_retries)


def live_orchestrator(
    config: Config,
    store: Store,
    submissions: Mailbox,
    conversation: Mailbox,
    clock: Clock | None = None,
    lock: asyncio.Lock | None = None,
) -> Orchestrator:
    """The orchestrator as main builds it, with the gateway key from the environment."""
    outbox = build_outbox(config, conversation, store)
    operations = build_operations(config, store, submissions, outbox, clock or SystemClock())
    llm_key = os.environ[config.llm.api_key_env]
    return build_orchestrator(config, store, operations, llm_key, lock or asyncio.Lock())


async def conversation_id(
    config: Config, client: httpx.AsyncClient, address: str, message_id: str
) -> str:
    """The Exchange conversation a message belongs to, which every reply in a thread shares."""
    response = await _request(
        config,
        client,
        "GET",
        f"/users/{address}/messages/{quote(message_id, safe='')}",
        params={"$select": "conversationId"},
    )
    found: str = response.json()["conversationId"]
    return found


async def place_in_inbox(
    config: Config, client: httpx.AsyncClient, address: str, message: dict[str, Any]
) -> None:
    """Create a corpus message in the mailbox's inbox as received mail from its own sender.

    Staff senders take the first configured sender domain, so the corpus suits any tenant;
    outside senders keep theirs. Sending could only come from a Pulse mailbox, and Graph sets
    only X- headers on creation.
    """
    local, _, domain = message["sender_address"].rpartition("@")
    if domain == STAFF_DOMAIN:
        domain = config.allowed_sender_domains[0]
    sender = {"emailAddress": {"name": message["sender_name"], "address": f"{local}@{domain}"}}
    body: dict[str, Any] = {
        "subject": message["subject"],
        "from": sender,
        "sender": sender,
        "toRecipients": [{"emailAddress": {"address": address}}],
        "body": {"contentType": "Text", "content": message["body"]},
        "singleValueExtendedProperties": _RECEIVED,
    }
    if message.get("has_attachments"):
        content = base64.b64encode(b"Synthetic attachment for the test corpus.").decode()
        body["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": "notes.txt",
                "contentBytes": content,
            }
        ]
    await _request(
        config, client, "POST", f"/users/{address}/mailFolders/inbox/messages", json=body
    )


async def _request(
    config: Config, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    token = await _credential(config).token()
    headers = {"Authorization": f"Bearer {token}", "Prefer": 'IdType="ImmutableId"'}
    response = await client.request(method, url, headers=headers, **kwargs)
    response.raise_for_status()
    return response


def _credential(config: Config) -> CertificateCredential:
    graph = config.graph
    return CertificateCredential(graph.tenant_id, graph.client_id, graph.certificate_path)
