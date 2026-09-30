"""Runs the orchestrator through the dev gateway, using ./config.yaml and the keys in .env."""

import os
import re
from pathlib import Path

import httpx
import pytest

from pulse.adapters.clock import SystemClock
from pulse.adapters.graph import GRAPH_URL
from pulse.adapters.store import SqliteStore
from pulse.config import load_config
from pulse.main import build_orchestrator
from tests.emails import (
    CORPUS_ITEM_SOURCES,
    CORPUS_OUTCOMES,
    sendable_corpus_messages,
    stored_outcomes,
)
from tests.fakes.mailbox import FakeMailbox
from tests.live import graph_mailbox
from tests.messages import make_message, make_newsletter

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_a_reviewer_message_gets_a_reply(tmp_path: Path) -> None:
    config = load_config(Path("config.yaml"))
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    await store.save_start(make_newsletter(), [])
    orchestrator = build_orchestrator(
        config, store, FakeMailbox(), os.environ[config.llm.api_key_env], SystemClock()
    )

    reply = await orchestrator.handle(make_message(text="Hello, what can you do for me?"))

    print(reply)
    assert reply
    assert len(await store.load_history("n-1")) == 2


# Graph's immutable message IDs, which reviewers should never see.
_MESSAGE_ID = re.compile(r"AAkAL[A-Za-z0-9_-]{20,}")


async def test_the_corpus_is_extracted_and_consolidated(tmp_path: Path) -> None:
    """The phase 5 check: needs the dev inbox to hold the sendable corpus once, as seeded."""
    config = load_config(Path("config.yaml"))
    corpus = {m["subject"]: m["id"] for m in sendable_corpus_messages()}
    # A temporary store, so the check never depends on or changes ./state.
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    async with httpx.AsyncClient(base_url=GRAPH_URL) as client:
        mailbox = graph_mailbox(config, client, config.mailboxes.submissions)
        inbox = sorted(email.subject for email in await mailbox.list_inbox())
        assert inbox == sorted(corpus), "reset the dev inbox and seed the corpus once"
        orchestrator = build_orchestrator(
            config, store, mailbox, os.environ[config.llm.api_key_env], SystemClock()
        )

        drafted = await orchestrator.handle(make_message("r01", text="Please draft a newsletter."))
        shown = await orchestrator.handle(
            make_message("r02", text="Show me the items and which emails each came from.")
        )

    print(f"--- reply 1\n{drafted}\n--- reply 2\n{shown}")
    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    emails = await store.list_screened_emails(newsletter.newsletter_id)
    corpus_id = {email.message_id: corpus[email.subject] for email in emails}
    outcomes = await stored_outcomes(store, newsletter.newsletter_id)
    assert {corpus_id[m]: outcome for m, outcome in outcomes.items()} == {
        m: CORPUS_OUTCOMES[m] for m in corpus.values()
    }
    items = await store.get_items(newsletter.newsletter_id)
    assert items is not None
    sources = [sorted(corpus_id[m] for m in item.source_message_ids) for item in items.items]
    assert sorted(sources) == CORPUS_ITEM_SOURCES
    for reply in (drafted, shown):
        assert reply is not None
        assert not _MESSAGE_ID.search(reply)
