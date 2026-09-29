import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.runner import AgentRunner
from pulse.config import Config
from pulse.conversation.email import NO_REPLY, poll_once
from pulse.conversation.runner import ConversationRunner
from pulse.models import Feedback
from pulse.pipeline.runner import run_build
from pulse.store import EditionStore

from ..support import REVIEWER, FakeMailbox, at, calling, message, seed_edition
from .conftest import agent_runner, clock, revise_stand_ins

pytestmark = pytest.mark.anyio


async def poll(
    config: Config,
    store: EditionStore,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    agents: AgentRunner,
) -> None:
    runner = ConversationRunner(
        config, store, submissions, conversation, agents, clock(), run_build
    )
    await poll_once(config, conversation, store, runner)


def from_reviewer(
    body: str, sender: str = REVIEWER, headers: dict[str, str] | None = None
) -> FakeMailbox:
    return FakeMailbox([message(id="m1", sender_address=sender, body=body, headers=headers)])


def failing_model() -> FunctionModel:
    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m")

    return FunctionModel(fail)


async def test_non_reviewer_is_moved_to_rejected(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    conversation = from_reviewer("Hello", sender="outsider@example.com")
    await poll(config, store, submissions, conversation, agent_runner(config, chat=failing_model()))
    assert conversation.folders["Rejected"] == ["m1"]
    assert conversation.replies == []


async def test_reviewer_address_matches_ignoring_case(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    conversation = from_reviewer("Hi", sender="Reviewer@Techary.AI")
    await poll(config, store, submissions, conversation, agent_runner(config, chat=calling("hi")))
    assert conversation.folders["Processed"] == ["m1"]
    assert [f.text for f in await store.feedback(edition_id)] == ["Hi"]


async def test_automatic_reply_is_moved_to_rejected(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    conversation = from_reviewer("Away", headers={"auto-submitted": "auto-replied"})
    await poll(config, store, submissions, conversation, agent_runner(config, chat=failing_model()))
    assert conversation.folders["Rejected"] == ["m1"]
    assert conversation.replies == []


async def test_no_reply_records_feedback_and_sends_nothing(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    conversation = from_reviewer("Thanks, looks good.")
    await poll(
        config, store, submissions, conversation, agent_runner(config, chat=calling(NO_REPLY))
    )
    assert conversation.folders["Processed"] == ["m1"]
    assert conversation.replies == []
    assert [f.text for f in await store.feedback(edition_id)] == ["Thanks, looks good."]


async def test_plain_reply_is_escaped_and_sent_to_reviewers_only(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    await seed_edition(store)
    conversation = from_reviewer("Status?")
    agents = agent_runner(config, chat=calling("It's <b>in review</b>."))
    await poll(config, store, submissions, conversation, agents)
    [reply] = conversation.replies
    assert reply.message_id == "m1" and reply.to == config.reviewers
    assert "&lt;b&gt;in review&lt;/b&gt;" in reply.html


@pytest.mark.parametrize("final_reply", ["Version 2 saved.", NO_REPLY])
async def test_reply_carries_a_new_version_even_with_no_reply(
    config: Config, store: EditionStore, submissions: FakeMailbox, final_reply: str
) -> None:
    edition_id = await seed_edition(store)
    conversation = from_reviewer("Please tidy the wording.")
    agents = revise_stand_ins(
        config, chat=calling(("revise_draft", {"instruction": "Tidy the wording"}), final_reply)
    )
    await poll(config, store, submissions, conversation, agents)

    assert conversation.folders["Processed"] == ["m1"]
    [reply] = conversation.replies
    assert "tidied up" in reply.html and "Tidied the wording" in reply.html
    assert (await store.current_version(edition_id)).number == 2


async def test_a_failed_turn_leaves_the_message_for_retry(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    await seed_edition(store)
    conversation = from_reviewer("Hello")
    await poll(config, store, submissions, conversation, agent_runner(config, chat=failing_model()))
    assert "m1" in conversation.inbox and conversation.folders == {}

    await poll(config, store, submissions, conversation, agent_runner(config, chat=calling("hi")))
    assert conversation.folders["Processed"] == ["m1"]


async def test_a_failed_reply_leaves_the_message_for_retry_with_nothing_saved(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    conversation = from_reviewer("Hello")
    conversation.fail_send = True
    await poll(config, store, submissions, conversation, agent_runner(config, chat=calling("hi")))
    assert "m1" in conversation.inbox and conversation.folders == {}
    assert await store.feedback(edition_id) == []


async def test_attempt_limit_moves_the_message_to_rejected(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    await seed_edition(store)
    conversation = from_reviewer("Hello")
    agents = agent_runner(config, chat=failing_model())
    for _ in range(config.chat.max_attempts - 1):
        await poll(config, store, submissions, conversation, agents)
        assert conversation.folders == {}
    await poll(config, store, submissions, conversation, agents)
    assert conversation.folders["Rejected"] == ["m1"]


async def test_recovery_after_a_crash_between_saving_and_moving(
    config: Config, store: EditionStore, submissions: FakeMailbox
) -> None:
    edition_id = await seed_edition(store)
    conversation = from_reviewer("Hello")
    # The turn was saved, with the message marked handled, but the move never happened.
    await store.save_turn(
        edition_id,
        [],
        Feedback(reviewer=REVIEWER, channel="email", text="Hello", received_at=at(25)),
        handled_message_id="m1",
    )
    await poll(config, store, submissions, conversation, agent_runner(config, chat=failing_model()))
    assert conversation.folders["Processed"] == ["m1"]
    assert conversation.replies == []
    assert len(await store.feedback(edition_id)) == 1
