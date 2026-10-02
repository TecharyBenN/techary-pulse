"""Calls the dev tenant through Graph, using ./config.yaml and the certificate it names."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from msgraph.graph_service_client import GraphServiceClient

from pulse.config import Config, load_config
from tests.emails import corpus_messages, seedable_corpus_messages
from tests.live import graph_mailbox, live_graph, place_in_inbox

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture
def config() -> Config:
    return load_config(Path("config.yaml"))


@pytest.fixture
async def client(config: Config) -> AsyncIterator[GraphServiceClient]:
    async with live_graph(config) as client:
        yield client


async def test_seed_the_corpus(config: Config, client: GraphServiceClient) -> None:
    """Place the seedable corpus in the submissions inbox, each message from its own sender."""
    seedable = seedable_corpus_messages()

    for message in seedable:
        await place_in_inbox(config, client, config.mailboxes.submissions, message)

    print(f"placed {len(seedable)} of {len(corpus_messages())} corpus messages")


async def test_list_the_submissions_inbox(config: Config, client: GraphServiceClient) -> None:
    emails = await graph_mailbox(config, client, config.mailboxes.submissions).list_inbox()

    print(f"{len(emails)} messages in the submissions inbox")
    assert all(email.message_id for email in emails)
