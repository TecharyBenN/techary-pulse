"""Runs the orchestrator through the dev gateway, using ./config.yaml and the keys in .env."""

import asyncio
import os
import re
from pathlib import Path

import httpx
import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ToolReturnPart

from pulse.adapters.clock import SystemClock
from pulse.adapters.graph import GRAPH_URL
from pulse.adapters.store import SqliteStore
from pulse.config import load_config
from pulse.main import build_orchestrator
from tests.emails import (
    CORPUS_ITEM_SOURCES,
    CORPUS_OUTCOMES,
    seedable_corpus_messages,
    stored_outcomes,
)
from tests.fakes.mailbox import FakeMailbox
from tests.live import conversation_id, graph_mailbox
from tests.messages import make_message, make_newsletter

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_a_reviewer_message_gets_a_reply(tmp_path: Path) -> None:
    config = load_config(Path("config.yaml"))
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    await store.save_start(make_newsletter(), [])
    orchestrator = build_orchestrator(
        config,
        store,
        FakeMailbox(),
        FakeMailbox(),
        os.environ[config.llm.api_key_env],
        SystemClock(),
        asyncio.Lock(),
    )

    reply = (await orchestrator.handle(make_message(text="Hello, what can you do for me?"))).text

    print(reply)
    assert reply
    assert len(await store.load_history("n-1")) == 2


# Graph's immutable message IDs, which reviewers should never see.
_MESSAGE_ID = re.compile(r"AAkAL[A-Za-z0-9_-]{20,}")


async def test_the_corpus_is_extracted_and_consolidated(tmp_path: Path) -> None:
    """The phase 5 check: needs the dev inbox to hold the seedable corpus once, as seeded."""
    config = load_config(Path("config.yaml"))
    corpus = {m["subject"]: m["id"] for m in seedable_corpus_messages()}
    # A temporary store, so the check never depends on or changes ./state.
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    async with httpx.AsyncClient(base_url=GRAPH_URL) as client:
        mailbox = graph_mailbox(config, client, config.mailboxes.submissions)
        inbox = sorted(email.subject for email in await mailbox.list_inbox())
        assert inbox == sorted(corpus), "reset the dev inbox and seed the corpus once"
        orchestrator = build_orchestrator(
            config,
            store,
            mailbox,
            FakeMailbox(),
            os.environ[config.llm.api_key_env],
            SystemClock(),
            asyncio.Lock(),
        )

        drafted = (
            await orchestrator.handle(make_message("r01", text="Please draft a newsletter."))
        ).text
        shown = (
            await orchestrator.handle(
                make_message("r02", text="Show me the items and which emails each came from.")
            )
        ).text

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


async def test_version_1_and_a_revised_version_2_reach_the_reviewers(tmp_path: Path) -> None:
    """The phase 6 check: needs the dev inbox to hold the seedable corpus, as seeded. The
    drafts are sent to the dev reviewers list, so the test user receives both."""
    config = load_config(Path("config.yaml"))
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    async with httpx.AsyncClient(base_url=GRAPH_URL) as client:
        orchestrator = build_orchestrator(
            config,
            store,
            graph_mailbox(config, client, config.mailboxes.submissions),
            graph_mailbox(config, client, config.mailboxes.conversation),
            os.environ[config.llm.api_key_env],
            SystemClock(),
            asyncio.Lock(),
        )

        first = await orchestrator.handle(make_message("r01", text="Please draft a newsletter."))
        opened = await store.get_open_newsletter()
        assert opened is not None and opened.thread_message_id is not None
        second = await orchestrator.handle(
            make_message("r02", text="Please make the intro shorter and friendlier.")
        )
        newsletter = await store.get_open_newsletter()
        assert newsletter is not None and newsletter.thread_message_id is not None
        # Version 2 is a reply in version 1's thread.
        conversation = config.mailboxes.conversation
        thread = [
            await conversation_id(config, client, conversation, message_id)
            for message_id in (opened.thread_message_id, newsletter.thread_message_id)
        ]

    drafted, revised = first.text, second.text
    print(f"--- reply 1\n{drafted}\n--- reply 2\n{revised}")
    assert thread[0] == thread[1]
    assert newsletter.latest_version == 2
    v1 = await store.get_version(newsletter.newsletter_id, 1)
    v2 = await store.get_version(newsletter.newsletter_id, 2)
    assert v1 is not None and v2 is not None
    # Each run carries the version it presented, so the chat endpoint shows its newsletter.
    assert (first.newsletter, second.newsletter) == (v1.content, v2.content)
    print(f"--- v1 intro\n{v1.content.intro}\n--- v2 intro\n{v2.content.intro}")
    assert v2.content.intro != v1.content.intro
    assert v2.changes
    for reply in (drafted, revised):
        assert reply is not None
        assert not _MESSAGE_ID.search(reply)


async def test_a_failing_check_is_fixed_or_listed_in_the_version(tmp_path: Path) -> None:
    """The phase 7 check: needs the dev inbox to hold the seedable corpus, as seeded. The
    drafts are sent to the dev reviewers list, so the test user receives them."""
    config = load_config(Path("config.yaml"))
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    async with httpx.AsyncClient(base_url=GRAPH_URL) as client:
        orchestrator = build_orchestrator(
            config,
            store,
            graph_mailbox(config, client, config.mailboxes.submissions),
            graph_mailbox(config, client, config.mailboxes.conversation),
            os.environ[config.llm.api_key_env],
            SystemClock(),
            asyncio.Lock(),
        )

        await orchestrator.handle(make_message("r01", text="Please draft a newsletter."))
        newsletter = await store.get_open_newsletter()
        assert newsletter is not None
        newsletter_id = newsletter.newsletter_id
        # The writer avoids dashes, so one is planted in the working draft for the run to meet.
        draft = await store.get_draft(newsletter_id)
        assert draft is not None
        dashed = draft.content.model_copy(
            update={"intro": f"{draft.content.intro} \N{EM DASH} with thanks to everyone."}
        )
        await store.save_draft(newsletter_id, draft.model_copy(update={"content": dashed}))
        presented = await orchestrator.handle(
            make_message(
                "r02",
                text="Please send the current working draft to the reviewers as a new version.",
            )
        )

    print(f"--- reply\n{presented.text}")
    newsletter = await store.get_open_newsletter()
    assert newsletter is not None and newsletter.latest_version == 2
    version = await store.get_version(newsletter_id, 2)
    assert version is not None
    print(f"--- intro\n{version.content.intro}\n--- failures\n{version.check_failures}")
    history = [
        message
        for row in await store.load_history(newsletter_id)
        if row.message_id == "r02"
        for message in ModelMessagesTypeAdapter.validate_json(row.data)
    ]
    checks = [
        part.content
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "check"
    ]
    print(f"--- check results\n{checks}")
    assert checks
    assert version.verdicts is not None
    # The planted dash was in the draft when the run started: a version without it was fixed.
    dash_in_intro = "\N{EM DASH}" in version.content.intro
    listed = any(f.check == "dashes" and f.target == "intro" for f in version.check_failures)
    assert dash_in_intro == listed
