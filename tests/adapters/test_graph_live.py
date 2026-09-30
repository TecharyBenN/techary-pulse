"""Calls the dev tenant through Graph, using ./config.yaml and the certificate it names."""

import html
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from pulse.adapters.graph import GRAPH_URL
from pulse.config import Config, load_config
from pulse.entities.mail import OutboundEmail
from tests.emails import corpus_messages, sendable_corpus_messages
from tests.live import graph_mailbox

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture
def config() -> Config:
    return load_config(Path("config.yaml"))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=GRAPH_URL) as client:
        yield client


async def test_seed_the_corpus(config: Config, client: httpx.AsyncClient) -> None:
    """Send the sendable corpus to the submissions mailbox from the conversation mailbox."""
    messages = corpus_messages()
    sender = graph_mailbox(config, client, config.mailboxes.conversation)
    sendable = sendable_corpus_messages()

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
    emails = await graph_mailbox(config, client, config.mailboxes.submissions).list_inbox()

    print(f"{len(emails)} messages in the submissions inbox")
    assert all(email.message_id for email in emails)
