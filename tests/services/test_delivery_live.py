"""The phase 8 and phase 10 checks: draft, approve and send newsletters through the dev gateway
and tenant, using ./config.yaml and the keys in .env. Each needs the dev inbox to hold the
seedable corpus, as seeded; the sent newsletter goes to the dev all-staff stand-in, and its
emails move to the Processed and Rejected folders, so reseed the corpus before the next check."""

import asyncio
import contextlib
import functools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from msgraph.graph_service_client import GraphServiceClient

from pulse.adapters.graph import GraphMailbox
from pulse.adapters.render import Renderer
from pulse.adapters.store import SqliteStore
from pulse.agents.orchestrator.run import Orchestrator
from pulse.config import Config, load_config
from pulse.main import build_delivery, build_outbox
from pulse.services.delivery import Delivery
from tests.emails import seedable_corpus_messages
from tests.live import (
    check_seeded_inbox,
    conversation_id,
    graph_mailbox,
    live_graph,
    live_orchestrator,
    sent_to,
)
from tests.messages import make_message

pytestmark = [pytest.mark.live, pytest.mark.anyio]


class _Live:
    """The orchestrator and delivery as main builds them, on a temporary store."""

    def __init__(
        self,
        client: GraphServiceClient,
        store: SqliteStore,
        submissions: GraphMailbox,
        orchestrator: Orchestrator,
        delivery: Delivery,
    ) -> None:
        self.client = client
        self.store = store
        self.submissions = submissions
        self.orchestrator = orchestrator
        self.delivery = delivery

    async def say(self, message_id: str, text: str) -> str | None:
        reply = (await self.orchestrator.handle(make_message(message_id, text=text))).text
        print(f"--- {text}\n{reply}")
        return reply


@contextlib.asynccontextmanager
async def _live(config: Config, tmp_path: Path, extra: int = 0) -> AsyncIterator[_Live]:
    """`extra` is how many inbox messages besides the seeded corpus the check expects."""
    assert config.send.mode == "on_approval", "the check expects the dev send mode"
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    clock = functools.partial(datetime.now, UTC)
    lock = asyncio.Lock()
    async with live_graph(config) as client:
        submissions = graph_mailbox(config, client, config.mailboxes.submissions)
        conversation = graph_mailbox(config, client, config.mailboxes.conversation)
        check_seeded_inbox(config, await submissions.list_inbox(), extra)
        orchestrator = live_orchestrator(config, store, submissions, conversation, clock, lock)
        renderer = Renderer(config.timezone)
        outbox = build_outbox(config, conversation, store, renderer)
        delivery = build_delivery(
            config, store, submissions, conversation, outbox, renderer, clock, lock
        )
        yield _Live(client, store, submissions, orchestrator, delivery)


async def test_an_approved_newsletter_is_withdrawn_then_sent(tmp_path: Path) -> None:
    config = load_config(Path("config.yaml"))
    async with _live(config, tmp_path) as live:
        await live.say("r01", "Please draft a newsletter.")
        await live.say("r02", "approve v1")
        approved = await live.store.get_open_newsletter()
        assert approved is not None and approved.state == "approved"

        await live.say("r03", "Please hold the send, I want to check one of the entries first.")
        await live.delivery.deliver()
        withdrawn = await live.store.get_open_newsletter()
        assert withdrawn is not None and withdrawn.state == "in_review"

        await live.say("r04", "approve v1")
        await live.delivery.deliver()
        sent = await live.store.get_latest_newsletter()
        assert sent is not None and sent.state == "sent"
        answer = await live.say("r05", "Has the newsletter been sent?")

        # Every notice and version is in the one email thread.
        assert approved.thread_message_id is not None and sent.thread_message_id is not None
        thread = {
            await conversation_id(live.client, config.mailboxes.conversation, message_id)
            for message_id in (approved.thread_message_id, sent.thread_message_id)
        }
        remaining = await live.submissions.list_inbox()

    assert len(thread) == 1
    emails = await live.store.list_screened_emails(sent.newsletter_id)
    records = {r.message_id for r in await live.store.list_extract_records(sent.newsletter_id)}
    moved = {e.message_id for e in emails if e.moved}
    assert moved == {e.message_id for e in emails if e.rejection or e.message_id in records}
    assert {e.message_id for e in remaining} == {e.message_id for e in emails} - moved
    assert answer is not None


async def test_flagged_entries_reach_the_reviewers_and_never_all_staff(tmp_path: Path) -> None:
    """Also needs the test user's forward of corpus m19's supplier email in the dev inbox."""
    config = load_config(Path("config.yaml"))
    assert "company_notices" in config.categories, "add the company_notices section"
    corpus = {m["subject"]: m["id"] for m in seedable_corpus_messages()}
    async with _live(config, tmp_path, extra=1) as live:
        await live.say("r01", "Please draft a newsletter.")
        newsletter = await live.store.get_open_newsletter()
        assert newsletter is not None
        newsletter_id = newsletter.newsletter_id
        version = await live.store.get_version(newsletter_id, 1)
        assert version is not None
        print(f"--- v1 notes\n{version.notes}")

        await live.say("r02", "approve v1")
        started = datetime.now(UTC)
        await live.delivery.deliver()
        sent = await live.store.get_latest_newsletter()
        assert sent is not None and sent.state == "sent"
        all_staff = await sent_to(
            live.client, config.mailboxes.conversation, config.all_staff, started
        )

    emails = await live.store.list_screened_emails(newsletter_id)
    by_corpus = {corpus[e.subject]: e.message_id for e in emails if e.subject in corpus}
    [forward] = [e.message_id for e in emails if e.subject not in corpus and e.rejection is None]
    records = {r.message_id: r for r in await live.store.list_extract_records(newsletter_id)}
    # Every email that passed the pre-filter was extracted.
    assert {e.message_id for e in emails if e.rejection is None} == set(records)
    assert records[forward].exclusion is None
    assert "financial" in {flag.kind for flag in records[forward].sensitivity}
    # Whoever forwards an email shares it; they are not part of the news.
    [sender] = [e.sender_name for e in emails if e.message_id == forward]
    assert sender not in records[forward].people
    for withheld in ("m22", "m23"):
        assert records[by_corpus[withheld]].exclusion == "sensitivity"
    items = await live.store.get_items(newsletter_id)
    assert items is not None

    def flagged(message_id: str) -> list[str]:
        [item] = [i for i in items.items if message_id in i.source_message_ids]
        return [flag.kind for flag in version.notes.flags.get(item.item_id, [])]

    assert "financial" in flagged(forward)
    # A partner's "for partners only" marking is labelled, and the entry stays in the version.
    assert "confidential" in flagged(forward)
    assert "named_person" in flagged(by_corpus["m05"])
    # Every entry is credited to whoever shared it, in the all-staff send too.
    assert all(version.notes.credits.get(e.item_id) for e in version.content.entries())
    assert "- " in all_staff
    for label in ("Named person", "Financial", "Flagged for review"):
        assert label not in all_staff
