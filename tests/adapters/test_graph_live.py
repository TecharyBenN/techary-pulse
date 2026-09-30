"""Calls the dev tenant through Graph, using ./config.yaml and the certificate it names."""

import html
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from pulse.adapters.graph import GRAPH_URL, CertificateCredential, GraphMailbox
from pulse.config import Config, load_config
from pulse.entities.mail import OutboundEmail
from tests.emails import corpus_messages

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture
def config() -> Config:
    return load_config(Path("config.yaml"))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=GRAPH_URL) as client:
        yield client


def _mailbox(config: Config, client: httpx.AsyncClient, address: str) -> GraphMailbox:
    graph = config.graph
    credential = CertificateCredential(graph.tenant_id, graph.client_id, graph.certificate_path)
    return GraphMailbox(client, credential.token, address, graph.max_retries)


async def test_seed_the_corpus(config: Config, client: httpx.AsyncClient) -> None:
    """Send the corpus to the submissions mailbox from the conversation mailbox.

    Messages whose case needs headers or an external sender cannot be sent from the tenant, so
    they are left to the unit tests.
    """
    messages = corpus_messages()
    sender = _mailbox(config, client, config.mailboxes.conversation)
    sendable = [
        m for m in messages if "headers" not in m and m["sender_address"].endswith("@techary.ai")
    ]

    for message in sendable:
        await sender.send(
            OutboundEmail(
                to=[config.mailboxes.submissions],
                subject=message["subject"],
                html=f"<p>{html.escape(message['body'])}</p>",
                reply_to=None,
            )
        )

    print(f"sent {len(sendable)} of {len(messages)} corpus messages")


async def test_list_the_submissions_inbox(config: Config, client: httpx.AsyncClient) -> None:
    emails = await _mailbox(config, client, config.mailboxes.submissions).list_inbox()

    print(f"{len(emails)} messages in the submissions inbox")
    assert all(email.message_id for email in emails)
