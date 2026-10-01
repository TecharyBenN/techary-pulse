"""Connections to the dev tenant for `live` tests, using ./config.yaml and the certificate it
names."""

import base64
from typing import Any
from urllib.parse import quote

import httpx

from pulse.adapters.graph import CertificateCredential, GraphMailbox
from pulse.config import Config
from tests.emails import STAFF_DOMAIN

# PR_MESSAGE_FLAGS set to read, so a created message is received mail, not an unsent draft.
_RECEIVED = [{"id": "Integer 0x0E07", "value": "1"}]


def graph_mailbox(config: Config, client: httpx.AsyncClient, address: str) -> GraphMailbox:
    return GraphMailbox(client, _credential(config).token, address, config.graph.max_retries)


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
