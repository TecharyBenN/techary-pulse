"""The phase 8 check: approves, withdraws and sends a newsletter through the dev gateway and
tenant, using ./config.yaml and the keys in .env. It needs the dev inbox to hold the seedable
corpus, as seeded; the sent newsletter goes to the dev all-staff stand-in, and its emails move
to the Processed and Rejected folders, so reseed the corpus afterwards."""

import asyncio
from pathlib import Path

import httpx
import pytest

from pulse.adapters.clock import SystemClock
from pulse.adapters.graph import GRAPH_URL
from pulse.adapters.store import SqliteStore
from pulse.config import load_config
from pulse.main import build_delivery, build_outbox
from tests.emails import seedable_corpus_messages
from tests.live import conversation_id, graph_mailbox, live_orchestrator
from tests.messages import make_message

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_an_approved_newsletter_is_withdrawn_then_sent(tmp_path: Path) -> None:
    config = load_config(Path("config.yaml"))
    assert config.send.mode == "on_approval", "the check expects the dev send mode"
    corpus = sorted(m["subject"] for m in seedable_corpus_messages())
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    clock = SystemClock()
    lock = asyncio.Lock()
    async with httpx.AsyncClient(base_url=GRAPH_URL) as client:
        submissions = graph_mailbox(config, client, config.mailboxes.submissions)
        conversation = graph_mailbox(config, client, config.mailboxes.conversation)
        inbox = sorted(email.subject for email in await submissions.list_inbox())
        assert inbox == corpus, "reset the dev inbox and seed the corpus once"
        orchestrator = live_orchestrator(config, store, submissions, conversation, clock, lock)
        outbox = build_outbox(config, conversation, store)
        delivery = build_delivery(config, store, submissions, conversation, outbox, clock, lock)

        async def say(message_id: str, text: str) -> str | None:
            reply = (await orchestrator.handle(make_message(message_id, text=text))).text
            print(f"--- {text}\n{reply}")
            return reply

        await say("r01", "Please draft a newsletter.")
        await say("r02", "approve v1")
        approved = await store.get_open_newsletter()
        assert approved is not None and approved.state == "approved"

        await say("r03", "Please hold the send, I want to check one of the entries first.")
        await delivery.deliver()
        withdrawn = await store.get_open_newsletter()
        assert withdrawn is not None and withdrawn.state == "in_review"

        await say("r04", "approve v1")
        await delivery.deliver()
        sent = await store.get_latest_newsletter()
        assert sent is not None and sent.state == "sent"
        answer = await say("r05", "Has the newsletter been sent?")

        # Every notice and version is in the one email thread.
        assert approved.thread_message_id is not None and sent.thread_message_id is not None
        thread = {
            await conversation_id(config, client, config.mailboxes.conversation, message_id)
            for message_id in (approved.thread_message_id, sent.thread_message_id)
        }
        remaining = await submissions.list_inbox()

    assert len(thread) == 1
    emails = await store.list_screened_emails(sent.newsletter_id)
    records = {r.message_id for r in await store.list_extract_records(sent.newsletter_id)}
    moved = {e.message_id for e in emails if e.moved}
    assert moved == {e.message_id for e in emails if e.rejection or e.message_id in records}
    assert {e.message_id for e in remaining} == {e.message_id for e in emails} - moved
    assert answer is not None
