import asyncio
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
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
from pydantic_ai.toolsets import FunctionToolset

from pulse.adapters.store import SqliteStore
from pulse.agents.orchestrator.agent import OrchestratorAgent
from pulse.agents.orchestrator.run import Orchestrator, RunReply, history_note
from pulse.entities.content import EntryNotes, Version, version_of
from pulse.entities.errors import MailboxError, RunFailed
from pulse.entities.extracts import Sensitivity
from pulse.entities.lifecycle import abandon, open_newsletter, present
from pulse.entities.store import DELIVERY_NOTE, HistoryRow
from tests.emails import (
    NO_NOTES,
    make_consolidation,
    make_draft,
    make_item,
    make_output,
    make_screened_email,
)
from tests.fakes.models import (
    ModelFunction,
    Tools,
    caller_call,
    gateway_error,
    make_orchestrator,
    ping_call,
    present_call,
    reply_with,
    responses,
    show_call,
    text_response,
)
from tests.messages import OPENED, REVIEWER, make_message, make_newsletter

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
    assert _prompts([m for _, m in history]) == [OrchestratorAgent.message(message)]
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
        OrchestratorAgent.message(make_message("r01", text="First")),
        OrchestratorAgent.message(make_message("r02", text="Second")),
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
    assert _prompts(seen[0]) == [OrchestratorAgent.message(message)]
    assert _feedback_ids(db_path) == ["r01"]
    history = await _history(store)
    assert [message_id for message_id, _ in history] == ["r01"] * 4
    assert [type(m) for _, m in history] == [ModelRequest, ModelResponse] * 2


async def test_resumes_unprocessed_tool_calls(store: SqliteStore) -> None:
    tools = Tools()
    message = make_message("r01")
    for saved in (
        ModelRequest(parts=[UserPromptPart(OrchestratorAgent.message(message))]),
        ping_call(),
    ):
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
    assert _prompts(seen[0]) == [OrchestratorAgent.message(make_message("r01", text="First"))]
    assert tools.calls == 1
    history = await _history(store)
    assert [message_id for message_id, _ in history] == ["r01"] * 4 + ["r02"] * 2


async def test_each_run_gives_its_tools_its_own_message(store: SqliteStore) -> None:
    tools = Tools()
    first = make_message("r01", text="First")
    await store.record_feedback("n-1", first)
    for saved in (
        ModelRequest(parts=[UserPromptPart(OrchestratorAgent.message(first))]),
        caller_call(),
    ):
        await store.append_history("n-1", "r01", ModelMessagesTypeAdapter.dump_json([saved]))
    model = responses(text_response("Resumed."), caller_call(), text_response("Done."))

    await make_orchestrator(store, model, tools).handle(make_message("r02", text="Second"))

    # The resumed run is r01's, so its tools act for r01's reviewer, not r02's.
    assert tools.callers == ["r01", "r02"]


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
        OrchestratorAgent.message(make_message("r01", text="First")),
        OrchestratorAgent.message(make_message("r02", text="Second")),
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


async def test_logs_a_failed_run_with_its_error(
    store: SqliteStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    with pytest.raises(RunFailed):
        await make_orchestrator(store, responses(gateway_error())).handle(make_message())

    run = _logged(caplog, "orchestrator_run")
    assert run["outcome"] == "failed"
    assert run["exc_info"][0].__name__ == "ModelHTTPError"  # type: ignore[index]


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
    agent = OrchestratorAgent(FunctionModel(model), FunctionToolset([opening_tool]))
    orchestrator = Orchestrator(agent, store, 40, timedelta(minutes=15), asyncio.Lock())

    reply = (await orchestrator.handle(make_message("r01", text="Please draft a newsletter."))).text

    assert reply == "Started."
    history = await store.load_history("n-9")
    assert [row.message_id for row in history] == ["r01"] * 4
    assert _prompts([_load(row) for row in history]) == [
        OrchestratorAgent.message(make_message("r01", text="Please draft a newsletter."))
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
    prompt = ModelRequest(parts=[UserPromptPart(OrchestratorAgent.message(make_message("r01")))])
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
    assert _prompts(seen)[0] == OrchestratorAgent.message(make_message("r01"))
    assert isinstance(seen[-2], ModelResponse)
    assert seen[-2].parts == [TextPart("Version 1 is with the reviewers.")]
    # The store keeps every result in full.
    assert _tool_results([m for _, m in await _history(store)])[0] == {"items": ["detail"]}


async def test_a_refused_present_draft_supersedes_nothing(store: SqliteStore) -> None:
    await _save(
        store,
        ModelRequest(parts=[UserPromptPart(OrchestratorAgent.message(make_message("r01")))]),
        *_tool_exchange("get_items", "c1", {"items": ["detail"]}),
        *_tool_exchange("present_draft", "c2", "Refused: there is no working draft"),
        text_response("There is no draft yet."),
    )

    seen = await _seen_history(store)

    assert _tool_results(seen) == [{"items": ["detail"]}, "Refused: there is no working draft"]


PRICE = Sensitivity(kind="financial", withheld=False, evidence="mentions a supplier's prices")


async def _version_1(store: SqliteStore) -> Version:
    """Store version 1, which the stand-in present_draft presents, with its entry flagged."""
    notes = EntryNotes(credits={"item-1": ["Priya Shah"]}, flags={"item-1": [PRICE]})
    version = version_of(make_draft(), 1, OPENED, [], None, notes)
    await store.save_version(present(make_newsletter()), version)
    return version


async def test_reply_carries_the_version_the_run_presented(store: SqliteStore) -> None:
    version = await _version_1(store)
    model = responses(present_call(), text_response("Version 1 is ready."))

    reply = await make_orchestrator(store, model, Tools()).handle(make_message())

    assert (reply.text, reply.newsletter) == ("Version 1 is ready.", version.content)
    assert reply.notes == version.notes


async def test_reply_carries_no_version_when_the_run_presented_none(store: SqliteStore) -> None:
    await _version_1(store)

    reply = await make_orchestrator(store, reply_with("Noted.")).handle(make_message())

    assert (reply.newsletter, reply.notes) == (None, NO_NOTES)


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
    # The draft has no items yet, so it has no notes.
    assert reply.notes == NO_NOTES


async def test_the_working_draft_shown_carries_its_notes_from_the_items_and_records(
    store: SqliteStore,
) -> None:
    await _version_1(store)
    await store.save_draft("n-1", make_draft(changes=["Shortened the intro"]))
    named = Sensitivity(kind="named_person", withheld=False, evidence="thanks a colleague by name")
    await store.save_extract("n-1", "m01", make_output(sensitivity=[named]))
    await store.save_items("n-1", make_consolidation(make_item(source_message_ids=["m01"])))
    await store.save_start(make_newsletter(), [make_screened_email("m01", sender_name="Dan Wood")])
    model = responses(show_call(), text_response("Here is the draft."))

    reply = await make_orchestrator(store, model, Tools()).handle(make_message())

    # Worked out when shown, not taken from version 1.
    assert reply.notes == EntryNotes(credits={"item-1": ["Dan Wood"]}, flags={"item-1": [named]})


async def _feedback(store: SqliteStore, newsletter_id: str) -> list[str]:
    return [message.message_id for message in await store.list_feedback(newsletter_id)]


async def _owners(store: SqliteStore, newsletter_id: str) -> list[str]:
    return [row.message_id for row in await store.load_history(newsletter_id)]


def _opening(store: SqliteStore, model: ModelFunction) -> Orchestrator:
    """An orchestrator whose one tool opens newsletter n-2, as start_newsletter would."""

    async def opening_tool() -> str:
        await store.save_start(open_newsletter("n-2", OPENED + timedelta(days=7)), [])
        return "opened"

    agent = OrchestratorAgent(FunctionModel(model), FunctionToolset([opening_tool]))
    return Orchestrator(agent, store, 40, timedelta(minutes=15), asyncio.Lock())


def _opening_call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("opening_tool", {})])


async def _close(store: SqliteStore) -> None:
    await store.save_newsletter(abandon(make_newsletter(), REVIEWER, OPENED + timedelta(days=1)))


async def test_the_conversation_continues_after_the_newsletter_closes(
    store: SqliteStore,
) -> None:
    await make_orchestrator(store, reply_with("Noted.")).handle(make_message("r01"))
    await _close(store)
    seen: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return text_response("It was abandoned.")

    reply = await make_orchestrator(store, model).handle(make_message("r02", text="Is it done?"))

    assert reply.text == "It was abandoned."
    assert _prompts(seen[0]) == [
        OrchestratorAgent.message(make_message("r01")),
        OrchestratorAgent.message(make_message("r02", text="Is it done?")),
    ]
    assert await _owners(store, "n-1") == ["r01", "r01", "r02", "r02"]
    assert await _feedback(store, "n-1") == ["r01", "r02"]


async def test_a_run_that_opens_a_newsletter_moves_its_exchange_to_it(
    store: SqliteStore,
) -> None:
    await make_orchestrator(store, reply_with("Noted.")).handle(make_message("r01"))
    await _close(store)
    message = make_message("r02", text="Please draft a new newsletter.")

    reply = await _opening(store, responses(_opening_call(), text_response("Started."))).handle(
        message
    )

    assert reply.text == "Started."
    history = [_load(row) for row in await store.load_history("n-2")]
    assert await _owners(store, "n-2") == ["r02"] * 4
    # The new newsletter's history is this run alone, from its prompt.
    assert _prompts(history) == [OrchestratorAgent.message(message)]
    assert await _feedback(store, "n-2") == ["r02"]
    # The earlier newsletter keeps its own exchange, with the run's steps before the opening.
    assert await _owners(store, "n-1") == ["r01", "r01", "r02", "r02"]


async def test_a_resumed_run_that_opens_a_newsletter_moves_its_own_exchange(
    store: SqliteStore,
) -> None:
    first = make_message("r01", text="Please draft a newsletter.")
    await store.record_feedback("n-1", first)
    await _save(
        store,
        ModelRequest(parts=[UserPromptPart(OrchestratorAgent.message(first))]),
        _opening_call(),
    )
    await _close(store)
    model = responses(text_response("Started."), text_response("Hello again."))

    reply = await _opening(store, model).handle(make_message("r02", text="Hello?"))

    assert reply.text == "Hello again."
    assert await _owners(store, "n-2") == ["r01"] * 4 + ["r02"] * 2
    assert await _feedback(store, "n-2") == ["r01", "r02"]
    history = [_load(row) for row in await store.load_history("n-2")]
    assert _prompts(history) == [
        OrchestratorAgent.message(first),
        OrchestratorAgent.message(make_message("r02", text="Hello?")),
    ]


async def test_a_retry_after_a_crash_following_the_opening_runs_in_the_new_newsletter(
    store: SqliteStore,
) -> None:
    await _close(store)
    message = make_message("r01", text="Please draft a new newsletter.")
    with pytest.raises(RunFailed):
        await _opening(store, responses(_opening_call(), gateway_error())).handle(message)

    reply = await _opening(store, reply_with("Done.")).handle(message)

    assert reply.text == "Done."
    assert await _feedback(store, "n-2") == ["r01"]
    assert _prompts([_load(row) for row in await store.load_history("n-2")]) == [
        OrchestratorAgent.message(message)
    ]


async def test_the_delivery_note_joins_the_next_prompt(store: SqliteStore) -> None:
    await make_orchestrator(store, reply_with("Noted.")).handle(make_message("r01"))
    note = "Pulse sent version 1 to all staff on 28 September 2026 at 09:00."
    await store.append_history("n-1", DELIVERY_NOTE, history_note(note))
    seen: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return text_response("Yes, it went out.")

    reply = await make_orchestrator(store, model).handle(make_message("r02", text="Sent?"))

    assert reply.text == "Yes, it went out."
    # The note is not an unfinished run, so nothing is resumed and it reaches the model once.
    assert len(seen) == 1
    *_, last = seen[0]
    assert isinstance(last, ModelRequest)
    assert [p.content for p in last.parts if isinstance(p, UserPromptPart)] == [
        note,
        OrchestratorAgent.message(make_message("r02", text="Sent?")),
    ]


async def test_the_delivery_note_after_an_unfinished_run_is_not_resumed(
    store: SqliteStore,
) -> None:
    first = make_message("r01")
    await store.record_feedback("n-1", first)
    await _save(
        store, ModelRequest(parts=[UserPromptPart(OrchestratorAgent.message(first))]), ping_call()
    )
    await store.append_history("n-1", DELIVERY_NOTE, history_note("Pulse sent version 1."))
    tools = Tools()

    reply = await make_orchestrator(store, reply_with("Done."), tools).handle(make_message("r02"))

    assert reply.text == "Done."
    assert tools.calls == 0


async def test_reply_carries_the_presented_version_for_the_reviewer_email(
    store: SqliteStore,
) -> None:
    version = await _version_1(store)
    model = responses(present_call(), text_response("Version 1 is ready."))

    reply = await make_orchestrator(store, model, Tools()).handle(make_message())

    assert reply.version == version


async def test_a_shown_version_is_not_a_presented_one(store: SqliteStore) -> None:
    await _version_1(store)
    await store.save_draft("n-1", make_draft())
    model = responses(show_call(), text_response("Here is the draft."))

    reply = await make_orchestrator(store, model, Tools()).handle(make_message())

    assert reply.newsletter is not None
    assert reply.version is None


async def test_a_message_whose_run_completed_gets_its_saved_reply_without_a_second_run(
    store: SqliteStore, db_path: Path
) -> None:
    version = await _version_1(store)
    message = make_message("c01", channel="email")
    model = responses(present_call(), text_response("Version 1 is ready."))
    await make_orchestrator(store, model, Tools()).handle(message)
    saved = await _history(store)

    # A second run would ask the model, which this stand-in refuses.
    reply = await make_orchestrator(store, responses(), Tools()).handle(message)

    assert (reply.text, reply.version) == ("Version 1 is ready.", version)
    assert await _history(store) == saved
    assert _feedback_ids(db_path) == ["c01"]


async def test_on_reply_receives_the_reply_while_the_lock_is_held(store: SqliteStore) -> None:
    lock = asyncio.Lock()
    delivered: list[tuple[str | None, bool]] = []

    async def on_reply(reply: RunReply) -> None:
        delivered.append((reply.text, lock.locked()))

    orchestrator = make_orchestrator(store, reply_with("Noted."), lock=lock)
    await orchestrator.handle(make_message(), on_reply=on_reply)

    assert delivered == [("Noted.", True)]
    assert not lock.locked()


async def test_a_failed_on_reply_raises_its_own_error(store: SqliteStore) -> None:
    async def on_reply(reply: RunReply) -> None:
        raise MailboxError("Graph returned HTTP 503")

    with pytest.raises(MailboxError):
        await make_orchestrator(store, reply_with("Noted.")).handle(
            make_message(), on_reply=on_reply
        )


def _recorded_prompts(seen: list[list[ModelMessage]]) -> list[object]:
    """The prompt each run sent, in order."""
    return [_prompts(messages)[-1] for messages in seen]


async def test_a_change_of_channel_asks_for_a_recap(store: SqliteStore) -> None:
    seen: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return text_response("Noted.")

    orchestrator = make_orchestrator(store, model)
    await orchestrator.handle(make_message("r01"))
    await orchestrator.handle(make_message("c02", channel="email"))
    await orchestrator.handle(make_message("c03", channel="email"))

    assert _recorded_prompts(seen) == [
        OrchestratorAgent.message(make_message("r01")),
        OrchestratorAgent.message(make_message("c02", channel="email"), recap=True),
        OrchestratorAgent.message(make_message("c03", channel="email")),
    ]


async def test_a_new_chat_asks_for_a_recap(store: SqliteStore) -> None:
    seen: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return text_response("Here is where things stand.")

    await make_orchestrator(store, model).handle(make_message(), new_chat=True)

    assert _recorded_prompts(seen) == [OrchestratorAgent.message(make_message(), recap=True)]


async def test_a_completed_run_returns_its_reply_before_resuming_another(
    store: SqliteStore,
) -> None:
    first = make_message("c01", channel="email")
    await make_orchestrator(store, reply_with("First reply.")).handle(first)
    # A later run stopped with its tool call unrun.
    unfinished = (
        ModelRequest(parts=[UserPromptPart(OrchestratorAgent.message(make_message("r02")))]),
        ping_call(),
    )
    for step in unfinished:
        await store.append_history("n-1", "r02", ModelMessagesTypeAdapter.dump_json([step]))
    await store.record_feedback("n-1", make_message("r02"))

    reply = await make_orchestrator(store, responses(), Tools()).handle(first)

    assert reply.text == "First reply."
