"""Calls the dev tenant through Graph, using ./config.yaml and the certificate it names."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from msgraph.graph_service_client import GraphServiceClient

from pulse.config import Config, load_config
from pulse.entities.mail import ScreenedEmail
from pulse.main import build_screen
from tests.emails import (
    GRAPH_CORPUS,
    SCREENED_EMAILS,
    corpus_messages,
    graph_corpus_emails,
    seedable_corpus_messages,
)
from tests.live import (
    as_corpus_email,
    check_seeded_inbox,
    graph_mailbox,
    live_graph,
    place_in_inbox,
)

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


async def test_the_corpus_as_graph_returns_it_matches_its_saved_copy(
    config: Config, client: GraphServiceClient
) -> None:
    """Needs the dev inbox to hold the seedable corpus once. A missing copy is written; review it
    before committing it."""
    inbox = await graph_mailbox(config, client, config.mailboxes.submissions).list_inbox()
    check_seeded_inbox(config, inbox)
    corpus_ids = {m["subject"]: m["id"] for m in seedable_corpus_messages()}
    screen = build_screen(config)
    emails = []
    for email in inbox:
        screened = screen(email)
        if email.subject in corpus_ids and screened.rejection is None:
            emails.append(as_corpus_email(config, screened, corpus_ids[email.subject]))
    emails.sort(key=lambda email: email.message_id)

    if not GRAPH_CORPUS.exists():
        GRAPH_CORPUS.write_bytes(SCREENED_EMAILS.dump_json(emails, indent=2) + b"\n")

    print(f"{len(emails)} corpus emails pass the pre-filter")
    assert [_text(email) for email in graph_corpus_emails()] == [_text(email) for email in emails]


def _text(email: ScreenedEmail) -> tuple[str, str, str | None, str | None]:
    return email.message_id, email.subject, email.unique_body, email.body
