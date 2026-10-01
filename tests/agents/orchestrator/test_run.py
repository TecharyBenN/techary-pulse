import asyncio
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.adapters.store import SqliteStore
from pulse.agents.orchestrator.agent import user_prompt
from pulse.agents.orchestrator.run import Orchestrator
from pulse.entities.content import Version, version_of
from pulse.entities.errors import RunFailed
from pulse.entities.lifecycle import present
from pulse.entities.store import HistoryRow
from tests.emails import make_draft
from tests.fakes.models import (
    Tools,
    gateway_error,
    make_orchestrator,
    ping_call,
    present_call,
    reply_with,
    responses,
    show_call,
    text_response,
)
from tests.messages import OPENED, make_message, make_newsletter

pytestmark = pytest.mark.anyio


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "pulse.db"


@pytest.fixture
async def store(db_path: Path) -> SqliteStore:
    store = SqliteStore(db_path)
    await store.initialise()
    await store.save_start(make_newsletter(), [])
    return store


async def _history(store: SqliteStore) -> list[tuple[str, ModelMessage]]:
    rows = await store.load_history("n-1")
    return [(row.message_id, _load(row)) for row in rows]


def _load(row: HistoryRow) -> ModelMessage:
    [message] = ModelMessagesTypeAdapter.validate_json(row.data)
    return message


def _prompts(messages: list[ModelMessage]) -> list[object]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


def _feedback_ids(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        return [row[0] for row in connection.execute("SELECT message_id FROM feedback")]


def _logged(caplog: pytest.LogCaptureFixture, event: str) -> dict[str, object]:
    """The fields of the first log entry for `event`."""
    return vars(next(r for r in caplog.records if r.getMessage() == event))


async def test_returns_thereply_with(store: SqliteStore) -> None:
    reply = (
        await make_orchestrator(store, reply_with("Happy to help.")).handle(make_message())
    ).text

    assert reply == "Happy to help."


async def test_records_feedback(store: SqliteStore, db_path: Path) -> None:
    await make_orchestrator(store, reply_with("Noted.")).handle(make_message("r01"))

    assert _feedback_ids(db_path) == ["r01"]


async def test_saves_each_step_with_the_message_id(store: SqliteStore) -> None:
    message = make_message("r01")

    await make_orchestrator(store, reply_with("Noted.")).handle(message)

    history = await _history(store)
    assert [message_id for message_id, _ in history] == ["r01", "r01"]
    assert _prompts([m for _, m in history]) == [user_prompt(message)]
    assert isinstance(history[1][1], ModelResponse)


async def test_saves_tool_calls_and_results(store: SqliteStore) -> None:
    tools = Tools()
    model = responses(ping_call(), text_response("Done."))

    await make_orchestrator(store, model, tools).handle(make_message())

    history = [m for _, m in await _history(store)]
    assert [type(m) for m in history] == [ModelRequest, ModelResponse, ModelRequest, ModelResponse]
    assert isinstance(history[2].parts[0], ToolReturnPart)
    assert history[2].parts[0].content == "pong"


async def test_passes_the_stored_history(store: SqliteStore) -> None:
    seen: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return text_response(f"Reply {len(seen)}")

    orchestrator = make_orchestrator(store, model)
    await orchestrator.handle(make_message("r01", text="First"))
    await orchestrator.handle(make_message("r02", text="Second"))

    second = seen[1]
    assert _prompts(second) == [
        user_prompt(make_message("r01", text="First")),
        user_prompt(make_message("r02", text="Second")),
    ]
    assert any(isinstance(m, ModelResponse) and m.parts == [TextPart("Reply 1")] for m in second)


async def test_without_an_open_newsletter_nothing_is_saved(tmp_path: Path) -> None:
    db_path = tmp_path / "pulse.db"
    store = SqliteStore(db_path)
    await store.initialise()

    reply = (await make_orchestrator(store, reply_with("Hello.")).handle(make_message())).text

    assert reply == "Hello."
    assert _feedback_ids(db_path) == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


@pytest.mark.parametrize("text", ["NO_REPLY", "  NO_REPLY\n"])
async def test_noreply_with(store: SqliteStore, db_path: Path, text: str) -> None:
    reply = (await make_orchestrator(store, reply_with(text)).handle(make_message())).text

    assert reply is None
    assert _feedback_ids(db_path) == ["r01"]


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
async def test_truncated_or_refused_reply_fails(store: SqliteStore, finish_reason: str) -> None:
    response = ModelResponse(parts=[TextPart("Partial")], finish_reason=finish_reason)  # type: ignore[arg-type]

    with pytest.raises(RunFailed):
        await make_orchestrator(store, responses(response)).handle(make_message())


async def test_gateway_error_fails(store: SqliteStore) -> None:
    with pytest.raises(RunFailed):
        await make_orchestrator(store, responses(gateway_error())).handle(make_message())


async def test_tool_call_limit(store: SqliteStore) -> None:
    tools = Tools()
    model = responses(ping_call(), ping_call(), text_response("Done."))

    with pytest.raises(RunFailed):
        await make_orchestrator(store, model, tools, max_tool_calls=1).handle(make_message())

    assert tools.calls == 1
    history = [m for _, m in await _history(store)]
    assert [type(m) for m in history] == [ModelRequest, ModelResponse, ModelRequest, ModelResponse]


async def test_time_limit(store: SqliteStore) -> None:
    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(10)
        return text_response("Too late.")

    orchestrator = make_orchestrator(store, slow, max_run_time=timedelta(milliseconds=50))

    with pytest.raises(RunFailed):
        await orchestrator.handle(make_message())

    assert [type(m) for _, m in await _history(store)] == [ModelRequest]


async def test_progress_note_for_each_tool_call(store: SqliteStore) -> None:
    notes: list[str] = []

    async def on_progress(note: str) -> None:
        notes.append(note)

    model = responses(ping_call(), ping_call(), text_response("Done."))

    await make_orchestrator(store, model, Tools()).handle(make_message(), on_progress)

    assert notes == ["Working on it (ping)", "Working on it (ping)"]


async def test_retried_message_resumes_from_saved_steps(store: SqliteStore, db_path: Path) -> None:
    tools = Tools()
    message = make_message("r01")
    with pytest.raises(RunFailed):
        await make_orchestrator(store, responses(ping_call(), gateway_error()), tools).handle(
            message
        )
    seen: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return text_response("Done.")

    reply = (await make_orchestrator(store, model, tools).handle(message)).text

    assert reply == "Done."
    assert tools.calls == 1
    assert _prompts(seen[0]) == [user_prompt(message)]
    assert _feedback_ids(db_path) == ["r01"]
    history = await _history(store)
    assert [message_id for message_id, _ in history] == ["r01"] * 4
    assert [type(m) for _, m in history] == [ModelRequest, ModelResponse] * 2


async def test_resumes_unprocessed_tool_calls(store: SqliteStore) -> None:
    tools = Tools()
    message = make_message("r01")
    for saved in (ModelRequest(parts=[UserPromptPart(user_prompt(message))]), ping_call()):
        await store.append_history("n-1", "r01", ModelMessagesTypeAdapter.dump_json([saved]))

    reply = (
        await make_orchestrator(store, responses(text_response("Done.")), tools).handle(message)
    ).text

    assert reply == "Done."
    assert tools.calls == 1
    history = [m for _, m in await _history(store)]
    assert [type(m) for m in history] == [ModelRequest, ModelResponse] * 2


async def test_unfinished_run_is_resumed_before_a_new_message(store: SqliteStore) -> None:
    tools = Tools()
    with pytest.raises(RunFailed):
        await make_orchestrator(store, responses(ping_call(), gateway_error()), tools).handle(
            make_message("r01", text="First")
        )
    seen: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return text_response(f"Reply {len(seen)}")

    reply = (
        await make_orchestrator(store, model, tools).handle(make_message("r02", text="Second"))
    ).text

    assert reply == "Reply 2"
    assert _prompts(seen[0]) == [user_prompt(make_message("r01", text="First"))]
    assert tools.calls == 1
    history = await _history(store)
    assert [message_id for message_id, _ in history] == ["r01"] * 4 + ["r02"] * 2


async def test_runs_are_processed_one_at_a_time_in_arrival_order(store: SqliteStore) -> None:
    gate = asyncio.Event()
    started: list[object] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        started.append(_prompts(messages)[-1])
        await gate.wait()
        return text_response("Done.")

    orchestrator = make_orchestrator(store, model)
    first = asyncio.create_task(orchestrator.handle(make_message("r01", text="First")))
    second = asyncio.create_task(orchestrator.handle(make_message("r02", text="Second")))
    while not started:
        await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0)

    assert len(started) == 1
    gate.set()
    await asyncio.gather(first, second)
    assert started == [
        user_prompt(make_message("r01", text="First")),
        user_prompt(make_message("r02", text="Second")),
    ]


async def test_logs_the_run_and_each_tool_call(
    store: SqliteStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    model = responses(ping_call(), text_response("Private reply text."))

    await make_orchestrator(store, model, Tools()).handle(make_message("r01"))

    tool = _logged(caplog, "tool_call")
    assert (tool["tool"], tool["outcome"]) == ("ping", "returned")
    assert isinstance(tool["duration_ms"], int)
    run = _logged(caplog, "orchestrator_run")
    assert (run["conversation_id"], run["message_id"], run["channel"], run["outcome"]) == (
        "n-1",
        "r01",
        "librechat",
        "reply",
    )
    assert "Private reply text." not in caplog.text


async def test_logs_a_failed_run_with_its_error_type(
    store: SqliteStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    with pytest.raises(RunFailed):
        await make_orchestrator(store, responses(gateway_error())).handle(make_message())

    run = _logged(caplog, "orchestrator_run")
    assert (run["outcome"], run["error_type"]) == ("failed", "ModelHTTPError")


async def test_exchange_that_opens_a_newsletter_becomes_its_history(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pulse.db"
    store = SqliteStore(db_path)
    await store.initialise()

    async def opening_tool() -> str:
        await store.save_start(make_newsletter("n-9"), [])
        return "opened"

    model = responses(
        ModelResponse(parts=[ToolCallPart("opening_tool", {})]), text_response("Started.")
    )
    agent = Agent(FunctionModel(model), output_type=str, tools=[opening_tool])
    orchestrator = Orchestrator(agent, store, 40, timedelta(minutes=15))

    reply = (await orchestrator.handle(make_message("r01", text="Please draft a newsletter."))).text

    assert reply == "Started."
    history = await store.load_history("n-9")
    assert [row.message_id for row in history] == ["r01"] * 4
    assert _prompts([_load(row) for row in history]) == [
        user_prompt(make_message("r01", text="Please draft a newsletter."))
    ]
    assert _feedback_ids(db_path) == ["r01"]


async def _save(store: SqliteStore, *messages: ModelMessage) -> None:
    for message in messages:
        await store.append_history("n-1", "r01", ModelMessagesTypeAdapter.dump_json([message]))


def _tool_exchange(tool: str, call_id: str, content: object) -> list[ModelMessage]:
    return [
        ModelResponse(parts=[ToolCallPart(tool, {}, call_id)]),
        ModelRequest(parts=[ToolReturnPart(tool, content, call_id)]),
    ]


def _tool_results(messages: list[ModelMessage]) -> list[object]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


async def _seen_history(store: SqliteStore) -> list[ModelMessage]:
    seen: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return text_response("Done.")

    await make_orchestrator(store, model).handle(make_message("r02", text="What changed?"))
    return seen[0]


async def test_results_before_the_latest_version_are_replaced_by_placeholders(
    store: SqliteStore,
) -> None:
    prompt = ModelRequest(parts=[UserPromptPart(user_prompt(make_message("r01")))])
    await _save(
        store,
        prompt,
        *_tool_exchange("get_items", "c1", {"items": ["detail"]}),
        *_tool_exchange("present_draft", "c2", {"version": 1}),
        *_tool_exchange("get_draft", "c3", {"content": "detail"}),
        text_response("Version 1 is with the reviewers."),
    )

    seen = await _seen_history(store)

    assert _tool_results(seen) == [
        "Earlier get_items result, superseded.",
        {"version": 1},
        {"content": "detail"},
    ]
    assert _prompts(seen)[0] == user_prompt(make_message("r01"))
    assert isinstance(seen[-2], ModelResponse)
    assert seen[-2].parts == [TextPart("Version 1 is with the reviewers.")]
    # The store keeps every result in full.
    assert _tool_results([m for _, m in await _history(store)])[0] == {"items": ["detail"]}


async def test_a_refused_present_draft_supersedes_nothing(store: SqliteStore) -> None:
    await _save(
        store,
        ModelRequest(parts=[UserPromptPart(user_prompt(make_message("r01")))]),
        *_tool_exchange("get_items", "c1", {"items": ["detail"]}),
        *_tool_exchange("present_draft", "c2", "Refused: there is no working draft"),
        text_response("There is no draft yet."),
    )

    seen = await _seen_history(store)

    assert _tool_results(seen) == [{"items": ["detail"]}, "Refused: there is no working draft"]


async def _version_1(store: SqliteStore) -> Version:
    """Store version 1, which the stand-in present_draft presents."""
    version = version_of(make_draft(), 1, OPENED)
    await store.save_version(present(make_newsletter()), version)
    return version


async def test_reply_carries_the_version_the_run_presented(store: SqliteStore) -> None:
    version = await _version_1(store)
    model = responses(present_call(), text_response("Version 1 is ready."))

    reply = await make_orchestrator(store, model, Tools()).handle(make_message())

    assert (reply.text, reply.newsletter) == ("Version 1 is ready.", version.content)


async def test_reply_carries_no_version_when_the_run_presented_none(store: SqliteStore) -> None:
    await _version_1(store)

    reply = await make_orchestrator(store, reply_with("Noted.")).handle(make_message())

    assert reply.newsletter is None


async def test_retried_message_carries_the_version_its_earlier_attempt_presented(
    store: SqliteStore,
) -> None:
    version = await _version_1(store)
    message = make_message("r01")
    with pytest.raises(RunFailed):
        await make_orchestrator(store, responses(present_call(), gateway_error()), Tools()).handle(
            message
        )

    reply = await make_orchestrator(store, reply_with("Done."), Tools()).handle(message)

    assert reply.newsletter == version.content


async def test_a_later_message_does_not_carry_an_earlier_version(store: SqliteStore) -> None:
    await _version_1(store)
    model = responses(present_call(), text_response("Version 1 is ready."))
    await make_orchestrator(store, model, Tools()).handle(make_message("r01"))

    reply = await make_orchestrator(store, reply_with("Noted.")).handle(make_message("r02"))

    assert reply.newsletter is None


async def test_reply_carries_the_working_draft_the_run_showed(store: SqliteStore) -> None:
    await _version_1(store)
    shown = make_draft(changes=["Shortened the intro"])
    await store.save_draft("n-1", shown)
    model = responses(show_call(), text_response("Here is the draft."))

    reply = await make_orchestrator(store, model, Tools()).handle(make_message())

    assert (reply.text, reply.newsletter) == ("Here is the draft.", shown.content)
