import asyncio

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.runner import AgentRunner
from pulse.config import Config
from pulse.conversation.runner import ConversationRunner, TurnResult
from pulse.errors import ConversationError
from pulse.models import ReviewerMessage
from pulse.pipeline.runner import run_build
from pulse.store import EditionStore

from ..support import REVIEWER, FakeMailbox, MailboxFailure, at, calling, seed_edition
from .conftest import agent_runner, build_stand_ins, build_submission, clock

pytestmark = pytest.mark.anyio


def runner(
    config: Config,
    store: EditionStore,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    agents: AgentRunner,
) -> ConversationRunner:
    return ConversationRunner(config, store, submissions, conversation, agents, clock(), run_build)


def incoming(text: str = "Hello", reviewer: str = REVIEWER) -> ReviewerMessage:
    return ReviewerMessage(
        reviewer=reviewer,
        reviewer_name="A Reviewer",
        channel="email",
        text=text,
        received_at=at(25),
    )


async def ignore(result: TurnResult) -> None:
    pass


def failing_model() -> FunctionModel:
    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m")

    return FunctionModel(fail)


async def test_non_reviewer_gets_no_agent_run_and_nothing_delivered(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    delivered: list[TurnResult] = []

    async def deliver(result: TurnResult) -> None:
        delivered.append(result)

    r = runner(config, store, submissions, conversation, agent_runner(config, chat=failing_model()))
    result = await r.run_turn(incoming(reviewer="other@techary.ai"), deliver)
    assert "is not a reviewer" in result.reply
    assert delivered == []


async def test_a_turn_is_delivered_then_saved_with_its_feedback(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    delivered: list[TurnResult] = []

    async def deliver(result: TurnResult) -> None:
        delivered.append(result)

    r = runner(config, store, submissions, conversation, agent_runner(config, chat=calling("hi")))
    result = await r.run_turn(incoming("What's the status?"), deliver, handled_message_id="m1")

    assert delivered == [result]
    assert result.edition_id == edition_id and result.reply == "hi"
    assert len(await store.load_history(edition_id)) >= 1
    assert [f.text for f in await store.feedback(edition_id)] == ["What's the status?"]
    assert await store.is_handled("m1")


async def test_an_empty_build_leaves_the_exchange_unsaved(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    agents = agent_runner(config, chat=calling(("build_newsletter", {}), "nothing to build"))
    r = runner(config, store, submissions, conversation, agents)
    result = await r.run_turn(incoming("Build the newsletter"), ignore, handled_message_id="m1")
    assert result.edition_id is None
    assert await store.open_edition() is None
    assert conversation.sent == []
    assert not await store.is_handled("m1")


async def test_an_exchange_with_no_open_edition_attaches_to_the_edition_it_builds(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    agents = build_stand_ins(config, chat=calling(("build_newsletter", {}), "built it"))
    r = runner(config, store, build_submission(), conversation, agents)

    result = await r.run_turn(incoming("Build the newsletter"), ignore)

    edition = await store.open_edition()
    assert edition is not None and edition.id == result.edition_id
    # The build's own "version 1 was sent" note, then this exchange's messages.
    assert len(await store.load_history(edition.id)) >= 2
    assert [f.text for f in await store.feedback(edition.id)] == ["Build the newsletter"]


async def test_gateway_error_raises_conversation_error_and_saves_nothing(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    r = runner(config, store, submissions, conversation, agent_runner(config, chat=failing_model()))
    with pytest.raises(ConversationError):
        await r.run_turn(incoming(), ignore)
    assert await store.load_history(edition_id) == []
    assert await store.feedback(edition_id) == []


async def test_failed_delivery_raises_conversation_error_and_saves_nothing(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)

    async def deliver(result: TurnResult) -> None:
        raise MailboxFailure("reply failed")

    r = runner(config, store, submissions, conversation, agent_runner(config, chat=calling("hi")))
    with pytest.raises(ConversationError):
        await r.run_turn(incoming(), deliver, handled_message_id="m1")
    assert await store.feedback(edition_id) == []
    assert not await store.is_handled("m1")


async def test_turns_run_one_at_a_time(
    config: Config, store: EditionStore, submissions: FakeMailbox, conversation: FakeMailbox
) -> None:
    await seed_edition(store)
    release = asyncio.Event()

    async def slow(result: TurnResult) -> None:
        await release.wait()

    r = runner(
        config, store, submissions, conversation, agent_runner(config, chat=calling("a", "b"))
    )
    first = asyncio.create_task(r.run_turn(incoming(), slow))
    await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(r.run_turn(incoming(), ignore), timeout=0.05)

    release.set()
    assert (await first).reply == "a"
    assert (await r.run_turn(incoming(), ignore)).reply == "b"
