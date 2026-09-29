from typing import Any

import pytest
from pydantic_ai.agent import AgentRunResult

from pulse.agents.chat import ChatDeps, chat_agent
from pulse.agents.runner import AgentRunner
from pulse.config import Config
from pulse.models import Channel, Draft
from pulse.pipeline.runner import run_build
from pulse.store import EditionStore

from ..support import (
    DRAFT_V1,
    REVIEWER,
    FakeMailbox,
    approve_seeded,
    calling,
    seed_edition,
)
from .conftest import agent_runner, build_stand_ins, build_submission, clock, revise_stand_ins

pytestmark = pytest.mark.anyio

OTHER = "other@techary.ai"


def deps(
    config: Config,
    store: EditionStore,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    *,
    caller: str = REVIEWER,
    channel: Channel = "email",
    text: str = "Hello",
    edition_id: str | None = None,
    agents: AgentRunner | None = None,
) -> ChatDeps:
    return ChatDeps(
        caller=caller,
        reviewer_name="A Reviewer",
        message=text,
        channel=channel,
        edition_id=edition_id,
        store=store,
        submissions=submissions,
        conversation=conversation,
        config=config,
        agents=agents or agent_runner(config),
        clock=clock(),
        run_build=run_build,
    )


async def call(tool: str, deps: ChatDeps, **args: Any) -> Any:
    """Run the chat agent calling one tool, and return what the tool returned."""
    result: AgentRunResult[str] = await chat_agent.run(
        "hi", deps=deps, model=calling((tool, args), "done")
    )
    return next(
        p.content
        for m in result.new_messages()
        for p in getattr(m, "parts", [])
        if getattr(p, "part_kind", "") == "tool-return"
    )


# get_edition


async def test_get_edition_with_no_open_edition(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    assert await call("get_edition", deps(config, store, submissions, conversation)) == {
        "open": False
    }


async def test_get_edition_returns_state_and_draft(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    edition = await call(
        "get_edition", deps(config, store, submissions, conversation, edition_id=edition_id)
    )
    assert edition["open"] is True
    assert edition["state"] == "in_review"
    assert edition["current_version"] == 1
    assert edition["headline"] == "A strong week"
    assert [i["item_id"] for i in edition["items"]] == ["item-1"]
    assert [e["reason"] for e in edition["excluded"]] == ["sensitivity"]


# build_newsletter


async def test_build_newsletter_reports_an_empty_build(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    reply = await call("build_newsletter", deps(config, store, submissions, conversation))
    assert "no pending submissions" in reply
    assert await store.open_edition() is None


async def test_build_newsletter_returns_the_open_edition_unchanged(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    d = deps(config, store, submissions, conversation, edition_id=edition_id)
    assert "already open" in await call("build_newsletter", d)
    assert conversation.sent == []
    assert d.new_edition_id == edition_id


async def test_build_newsletter_creates_an_edition_and_sets_new_edition_id(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    d = deps(config, store, build_submission(), conversation, agents=build_stand_ins(config))
    await call("build_newsletter", d)
    assert len(conversation.sent) == 1
    edition = await store.open_edition()
    assert edition is not None and edition.current_version == 1
    assert d.new_edition_id == edition.id


# resend_draft


async def test_resend_draft_with_no_open_edition(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    reply = await call("resend_draft", deps(config, store, submissions, conversation))
    assert "no open edition" in reply
    assert conversation.sent == []


async def test_resend_draft_emails_the_current_version_dated_by_the_edition(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    # The edition was created on 22 September; resending it on the 25th keeps that date.
    edition_id = await seed_edition(store)
    await call(
        "resend_draft", deps(config, store, submissions, conversation, edition_id=edition_id)
    )
    assert len(conversation.sent) == 1
    assert conversation.sent[0].to == config.reviewers
    assert conversation.sent[0].subject == "Draft v1: Pulse: 22 September 2026"


# approve


async def test_approve_records_approval_with_the_phrase_in_the_reviewers_message(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    d = deps(
        config, store, submissions, conversation, edition_id=edition_id, text="approve v1 please"
    )
    await call("approve", d, version=1)
    edition = await store.open_edition()
    assert edition is not None
    assert edition.state == "approved"
    assert edition.approved_version == 1
    assert edition.approver == REVIEWER


@pytest.mark.parametrize("text", ["looks good to me", "approve v12 please"])
async def test_approve_refused_without_the_exact_phrase_in_the_message(
    config: Config,
    store: EditionStore,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    text: str,
) -> None:
    edition_id = await seed_edition(store)
    d = deps(config, store, submissions, conversation, edition_id=edition_id, text=text)
    assert "does not contain 'approve v1'" in await call("approve", d, version=1)
    edition = await store.open_edition()
    assert edition is not None and edition.state == "in_review"


async def test_approve_refused_when_the_phrase_is_only_in_the_draft_not_the_message(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    # Only the reviewer's own message counts; text in the draft never supplies an approval.
    draft = Draft.model_validate(
        {**DRAFT_V1.model_dump(mode="json"), "intro": "Please approve v1 to publish."}
    )
    edition_id = await seed_edition(store, draft=draft)
    d = deps(config, store, submissions, conversation, edition_id=edition_id, text="thoughts?")
    assert "does not contain" in await call("approve", d, version=1)
    edition = await store.open_edition()
    assert edition is not None and edition.state == "in_review"


# withdraw_approval


async def test_withdraw_approval_returns_to_in_review_and_sends_a_notice(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    await approve_seeded(store)
    await call(
        "withdraw_approval", deps(config, store, submissions, conversation, edition_id=edition_id)
    )
    reread = await store.open_edition()
    assert reread is not None and reread.state == "in_review"
    assert [e.subject for e in conversation.sent] == ["Send cancelled: Pulse: 22 September 2026"]


async def test_withdraw_approval_refused_when_not_approved(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    reply = await call(
        "withdraw_approval", deps(config, store, submissions, conversation, edition_id=edition_id)
    )
    assert "not approved" in reply
    assert conversation.sent == []


async def test_withdraw_approval_refuses_a_non_reviewer(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    await approve_seeded(store)
    d = deps(config, store, submissions, conversation, edition_id=edition_id, caller=OTHER)
    assert "is not a reviewer" in await call("withdraw_approval", d)
    assert conversation.sent == []
    reread = await store.open_edition()
    assert reread is not None and reread.state == "approved"


# discard_edition


async def test_discard_edition_closes_it_and_sends_a_notice(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    await call(
        "discard_edition", deps(config, store, submissions, conversation, edition_id=edition_id)
    )
    assert await store.open_edition() is None
    assert [e.subject for e in conversation.sent] == ["Closed unsent: Pulse: 22 September 2026"]


async def test_discard_edition_refused_when_already_approved(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    await approve_seeded(store)
    reply = await call(
        "discard_edition", deps(config, store, submissions, conversation, edition_id=edition_id)
    )
    assert "not in_review" in reply
    assert conversation.sent == []


# revise_draft


async def test_revise_draft_saves_a_new_version_and_leaves_the_email_to_the_channel(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    d = deps(
        config,
        store,
        submissions,
        conversation,
        edition_id=edition_id,
        agents=revise_stand_ins(config),
    )
    assert "Tidied the wording" in await call("revise_draft", d, instruction="Tidy the wording")
    version = await store.current_version(edition_id)
    assert version.number == 2
    assert version.changes == ["Tidied the wording"]
    # In the email channel the reply-all carries the new version, so the tool sends nothing.
    assert conversation.sent == []
    assert d.version_email is not None
    assert d.version_email.subject == "Draft v2: Pulse: 22 September 2026"


async def test_revise_draft_emails_the_new_version_in_librechat(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    d = deps(
        config,
        store,
        submissions,
        conversation,
        edition_id=edition_id,
        agents=revise_stand_ins(config),
        channel="librechat",
    )
    await call("revise_draft", d, instruction="Tidy")
    assert conversation.sent == [d.version_email]


async def test_revise_draft_withdraws_approval_first_and_sends_a_notice(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    await approve_seeded(store)
    d = deps(
        config,
        store,
        submissions,
        conversation,
        edition_id=edition_id,
        agents=revise_stand_ins(config),
    )
    await call("revise_draft", d, instruction="Tidy")
    reread = await store.open_edition()
    assert reread is not None and reread.state == "in_review"
    assert [e.subject for e in conversation.sent] == ["Send cancelled: Pulse: 22 September 2026"]


async def test_revise_draft_refused_once_send_started(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    await approve_seeded(store, send_started=True)
    reply = await call(
        "revise_draft",
        deps(config, store, submissions, conversation, edition_id=edition_id),
        instruction="Tidy",
    )
    assert "already started" in reply
    assert conversation.sent == []
    assert (await store.current_version(edition_id)).number == 1


async def test_revise_draft_refuses_a_non_reviewer(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    d = deps(config, store, submissions, conversation, edition_id=edition_id, caller=OTHER)
    assert "is not a reviewer" in await call("revise_draft", d, instruction="Tidy")
    assert (await store.current_version(edition_id)).number == 1


async def test_reviewer_check_ignores_case(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    d = deps(
        config,
        store,
        submissions,
        conversation,
        edition_id=edition_id,
        caller="Reviewer@Techary.AI",
        agents=revise_stand_ins(config),
    )
    assert "is not a reviewer" not in await call("revise_draft", d, instruction="Tidy")
    assert (await store.current_version(edition_id)).number == 2
