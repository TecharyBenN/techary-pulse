import asyncio
import json
import sqlite3
from collections.abc import MutableMapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo
from starlette.requests import ClientDisconnect

from pulse.adapters.store import SqliteStore
from pulse.agents.orchestrator.run import Orchestrator
from pulse.entities.content import version_of
from pulse.entities.lifecycle import present
from pulse.entrypoints.chat import NOT_A_REVIEWER, PATH, create_app
from tests.emails import make_draft
from tests.fakes.clock import ControlledClock
from tests.fakes.models import (
    ModelFunction,
    Tools,
    gateway_error,
    make_orchestrator,
    ping_call,
    present_call,
    reply_with,
    responses,
    text_response,
)
from tests.messages import OPENED, make_message, make_newsletter
from tests.operations import make_renderer
from tests.tokens import NOW, REVIEWER_OID, REVIEWER_ROLE, make_token, make_verifier

pytestmark = pytest.mark.anyio

TOKEN = make_token()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "pulse.db"


@pytest.fixture
async def store(db_path: Path) -> SqliteStore:
    store = SqliteStore(db_path)
    await store.initialise()
    await store.save_start(make_newsletter(), [])
    return store


class Recorder:
    """A stand-in model that records the prompts it receives."""

    def __init__(self, reply: str = "Happy to help.") -> None:
        self.calls: list[list[ModelMessage]] = []
        self._reply = reply

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls.append(messages)
        return text_response(self._reply)


def _app(orchestrator: Orchestrator) -> FastAPI:
    clock = ControlledClock(NOW)
    return create_app(
        orchestrator, make_verifier(clock), REVIEWER_ROLE, clock, make_renderer().markdown
    )


def _body(text: str = "Hello", stream: bool = False) -> dict[str, Any]:
    return {"model": "pulse", "messages": [{"role": "user", "content": text}], "stream": stream}


async def _post(
    store: SqliteStore,
    model: ModelFunction,
    body: dict[str, Any],
    headers: dict[str, str] = HEADERS,
    tools: Tools | None = None,
) -> httpx.Response:
    app = _app(make_orchestrator(store, model, tools))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://pulse") as client:
        return await client.post(PATH, json=body, headers=headers)


def _events(response: httpx.Response) -> list[Any]:
    lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
    return [
        line.removeprefix("data: ")
        if line == "data: [DONE]"
        else json.loads(line.removeprefix("data: "))
        for line in lines
    ]


def _streamed_content(response: httpx.Response) -> str:
    return "".join(
        event["choices"][0]["delta"].get("content") or ""
        for event in _events(response)
        if isinstance(event, dict) and "choices" in event
    )


def _prompt_text(messages: list[ModelMessage]) -> str:
    return str(messages[-1].parts[-1].content)  # type: ignore[union-attr]


async def test_non_streamed_reply(store: SqliteStore) -> None:
    response = await _post(store, reply_with("Happy to help."), _body())

    assert response.status_code == 200
    completion = response.json()
    assert completion["id"].startswith("chatcmpl-")
    assert completion["object"] == "chat.completion"
    assert completion["created"] == int(NOW.timestamp())
    assert completion["model"] == "pulse"
    assert completion["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Happy to help."},
            "finish_reason": "stop",
        }
    ]


async def test_streamed_reply_follows_the_progress_notes(store: SqliteStore) -> None:
    model = responses(ping_call(), text_response("Done."))

    response = await _post(store, model, _body(stream=True), tools=Tools())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _streamed_content(response) == "Working on it (ping)\n\nDone."
    events = _events(response)
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert all(event["object"] == "chat.completion.chunk" for event in events[:-1])
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert events[-1] == "[DONE]"


async def _presented(store: SqliteStore) -> str:
    """Store version 1, which the stand-in present_draft presents; return its Markdown."""
    draft = make_draft()
    await store.save_version(present(make_newsletter()), version_of(draft, 1, OPENED, [], None))
    return make_renderer().markdown(draft.content)


@pytest.mark.parametrize("stream", [False, True])
async def test_presented_version_follows_the_reply_in_markdown(
    store: SqliteStore, stream: bool
) -> None:
    newsletter = await _presented(store)
    model = responses(present_call(), text_response("Version 1 is ready."))

    response = await _post(store, model, _body(stream=stream), tools=Tools())

    if stream:
        assert _streamed_content(response) == (
            "Sending the draft to the reviewers (present_draft)\n\n"
            f"Version 1 is ready.\n\n{newsletter}"
        )
    else:
        content = response.json()["choices"][0]["message"]["content"]
        assert content == f"Version 1 is ready.\n\n{newsletter}"


async def test_reply_without_a_presented_version_has_no_newsletter(store: SqliteStore) -> None:
    await _presented(store)

    response = await _post(store, reply_with("No changes yet."), _body())

    assert response.json()["choices"][0]["message"]["content"] == "No changes yet."


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer ",
        TOKEN,
        f"Basic {TOKEN}",
        "Bearer not-a-token",
        f"Bearer {make_token(signed_by_other_key=True)}",
        f"Bearer {make_token(exp=NOW - timedelta(seconds=1))}",
        f"Bearer {make_token(aud='another-api')}",
    ],
    ids=[
        "none",
        "empty",
        "no token",
        "no scheme",
        "wrong scheme",
        "malformed",
        "wrong key",
        "expired",
        "wrong audience",
    ],
)
async def test_requests_without_a_valid_token_are_refused(
    store: SqliteStore, authorization: str | None
) -> None:
    model = Recorder()
    headers = {} if authorization is None else {"Authorization": authorization}

    response = await _post(store, model, _body(), headers)

    assert response.status_code == 401
    assert model.calls == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("roles", [[], ["model.chat-anthropic"]])
async def test_caller_without_the_reviewer_role_is_forbidden(
    store: SqliteStore, stream: bool, roles: list[str]
) -> None:
    model = Recorder()
    # An identity header proves nothing, so it cannot make the caller a reviewer.
    headers = {
        "Authorization": f"Bearer {make_token(roles=roles)}",
        "X-User-Email": "testuser@example.org",
    }

    response = await _post(store, model, _body(stream=stream), headers)

    assert response.status_code == 403
    assert response.json()["error"] == {"message": NOT_A_REVIEWER, "type": "permission_error"}
    assert model.calls == []


async def test_only_the_newest_user_message_is_taken(store: SqliteStore) -> None:
    model = Recorder()
    body = _body()
    body["messages"] = [
        {"role": "system", "content": "Client system prompt"},
        {"role": "user", "content": "Old message"},
        {"role": "assistant", "content": "Old reply"},
        {"role": "user", "content": "Newest message"},
    ]

    await _post(store, model, body)

    [messages] = model.calls
    assert "Newest message" in _prompt_text(messages)
    assert "Old" not in "".join(str(m) for m in messages)
    assert "Client system prompt" not in "".join(str(m) for m in messages)


async def test_text_parts_of_the_newest_user_message_are_joined(store: SqliteStore) -> None:
    model = Recorder()
    body = _body()
    body["messages"] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "First part"},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                {"type": "text", "text": "Second part"},
            ],
        }
    ]

    await _post(store, model, body)

    [messages] = model.calls
    assert "First part\nSecond part" in _prompt_text(messages)


async def test_request_without_a_user_message_is_invalid(store: SqliteStore) -> None:
    model = Recorder()
    body = _body()
    body["messages"] = [{"role": "system", "content": "Only a system prompt"}]

    response = await _post(store, model, body)

    assert response.status_code == 400
    assert "error" in response.json()
    assert model.calls == []


async def test_reviewer_message_is_recorded_as_librechat_feedback(
    store: SqliteStore, db_path: Path
) -> None:
    await _post(store, reply_with("Noted."), _body("Shorter intro, please"))

    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT author, channel, text, received FROM feedback").fetchone()
    assert row == (REVIEWER_OID, "librechat", "Shorter intro, please", "2026-09-26T10:00:00Z")


@pytest.mark.parametrize("stream", [False, True])
async def test_no_reply_gives_empty_content(store: SqliteStore, stream: bool) -> None:
    response = await _post(store, reply_with("NO_REPLY"), _body(stream=stream))

    assert response.status_code == 200
    if stream:
        assert _streamed_content(response) == ""
    else:
        assert response.json()["choices"][0]["message"]["content"] == ""


async def test_failed_run_gives_an_error_response(store: SqliteStore) -> None:
    response = await _post(store, responses(gateway_error()), _body())

    assert response.status_code == 500
    assert response.json()["error"]["type"] == "server_error"
    assert "502" not in response.text


async def test_failed_streamed_run_ends_with_an_error_event(store: SqliteStore) -> None:
    response = await _post(store, responses(gateway_error()), _body(stream=True))

    events = _events(response)
    assert events[-2]["error"]["type"] == "server_error"
    assert events[-1] == "[DONE]"


async def test_client_disconnect_does_not_cancel_the_run(store: SqliteStore) -> None:
    entered = asyncio.Event()
    gate = asyncio.Event()
    finished: list[str] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        entered.set()
        await gate.wait()
        finished.append(_prompt_text(messages))
        return text_response("Done.")

    orchestrator = make_orchestrator(store, model)
    await _disconnect_after_first_chunk(_app(orchestrator), _body("Before", stream=True))
    await entered.wait()
    gate.set()

    # The lock runs this only once the disconnected request's run has finished.
    await orchestrator.handle(make_message("r-after"))

    assert len(finished) == 2
    assert "Before" in finished[0]


async def _disconnect_after_first_chunk(app: FastAPI, body: dict[str, Any]) -> None:
    """Drive the app over ASGI directly, failing the first body send as a gone client does."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")]
        + [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("pulse", 80),
    }
    request = {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    requests = [request]

    async def receive() -> MutableMapping[str, Any]:
        if requests:
            return requests.pop()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("the client has gone")

    with pytest.raises(ClientDisconnect):
        await app(scope, receive, send)
