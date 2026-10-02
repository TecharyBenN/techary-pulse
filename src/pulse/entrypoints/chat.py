"""The chat endpoint: create_app serves OpenAI chat completions in front of the orchestrator, and
serve runs it."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.shared import ErrorObject
from pydantic import BaseModel, ValidationError

from pulse.agents.orchestrator.run import Orchestrator, RunReply
from pulse.entities.auth import Caller
from pulse.entities.content import Content
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import InvalidToken, PulseError

PATH = "/v1/chat/completions"
NOT_A_REVIEWER = "You are not a Pulse reviewer."
_RUN_FAILED = "Pulse could not complete your request. Please try again."

_log = logging.getLogger(__name__)


class _ContentPart(BaseModel):
    type: str
    text: str | None = None


class _RequestMessage(BaseModel):
    role: str
    content: str | list[_ContentPart] | None = None

    def text(self) -> str:
        if isinstance(self.content, list):
            return "\n".join(p.text for p in self.content if p.type == "text" and p.text)
        return self.content or ""


class _ChatRequest(BaseModel):
    """The fields Pulse reads; clients send others, which are ignored."""

    model: str
    messages: list[_RequestMessage]
    stream: bool = False


class _Completion:
    """The ID, creation time and model that every part of one response shares."""

    def __init__(self, model: str, created: datetime) -> None:
        self.id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(created.timestamp())
        self.model = model

    def whole(self, content: str) -> ChatCompletion:
        message = ChatCompletionMessage(role="assistant", content=content)
        return ChatCompletion(
            id=self.id,
            object="chat.completion",
            created=self.created,
            model=self.model,
            choices=[Choice(index=0, message=message, finish_reason="stop")],
        )

    def chunk(self, delta: ChoiceDelta, finish_reason: Literal["stop"] | None = None) -> str:
        chunk = ChatCompletionChunk(
            id=self.id,
            object="chat.completion.chunk",
            created=self.created,
            model=self.model,
            choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish_reason)],
        )
        return _event(chunk)


def create_app(
    orchestrator: Orchestrator,
    verify: Callable[[str], Awaitable[Caller]],
    reviewer_role: str,
    clock: Callable[[], datetime],
    render_markdown: Callable[[Content], str],
) -> FastAPI:
    """`render_markdown` renders the newsletter a run presented or showed, after the reply."""
    app = FastAPI()
    # Runs outlive their requests, so a client that disconnects never cancels a run.
    running: set[asyncio.Task[RunReply]] = set()

    @app.post(PATH)
    async def chat_completions(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller = await _caller(authorization, verify)
        if caller is None:
            return _error(401, "invalid_request_error", "A valid bearer token is required.")
        if reviewer_role not in caller.roles:
            return _error(403, "permission_error", NOT_A_REVIEWER)
        try:
            chat = _ChatRequest.model_validate_json(await request.body())
        except ValidationError:
            return _error(400, "invalid_request_error", "The request is not a chat completion.")
        user_messages = [message for message in chat.messages if message.role == "user"]
        if not user_messages:
            return _error(400, "invalid_request_error", "The request has no user message.")

        message = ReviewerMessage(
            message_id=str(uuid.uuid4()),
            author=caller.oid,
            channel="librechat",
            text=user_messages[-1].text(),
            received=clock(),
        )
        notes: asyncio.Queue[str | None] = asyncio.Queue()
        # Only the count of the client's messages is read, so a new chat can open with a recap.
        new_chat = len(user_messages) == 1
        task = asyncio.create_task(orchestrator.handle(message, notes.put, new_chat=new_chat))
        running.add(task)

        def finished(task: asyncio.Task[RunReply]) -> None:
            running.discard(task)
            notes.put_nowait(None)
            # The run logs its own failure; retrieving it here stops asyncio logging it again.
            if not task.cancelled():
                task.exception()

        task.add_done_callback(finished)

        completion = _Completion(chat.model, message.received)
        if chat.stream:
            return StreamingResponse(
                _stream(task, notes, completion, render_markdown), media_type="text/event-stream"
            )
        try:
            reply = await asyncio.shield(task)
        except PulseError:
            return _error(500, "server_error", _RUN_FAILED)
        content = _content(reply, render_markdown)
        return JSONResponse(completion.whole(content).model_dump(mode="json", exclude_none=True))

    return app


async def serve(app: FastAPI, port: int) -> None:
    """Serve the app on Pulse's event loop, logging through Pulse's JSON log format."""
    # In a container, the gateway's connections arrive on the container's own interface.
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None)
    await uvicorn.Server(config).serve()


async def _stream(
    task: asyncio.Task[RunReply],
    notes: asyncio.Queue[str | None],
    completion: _Completion,
    render_markdown: Callable[[Content], str],
) -> AsyncIterator[str]:
    yield completion.chunk(ChoiceDelta(role="assistant"))
    while (note := await notes.get()) is not None:
        yield completion.chunk(ChoiceDelta(content=f"{note}\n\n"))
    try:
        reply = task.result()
    except PulseError:
        yield f"data: {json.dumps(_error_body('server_error', _RUN_FAILED))}\n\n"
    else:
        if content := _content(reply, render_markdown):
            yield completion.chunk(ChoiceDelta(content=content))
        yield completion.chunk(ChoiceDelta(), finish_reason="stop")
    yield "data: [DONE]\n\n"


def _content(reply: RunReply, render_markdown: Callable[[Content], str]) -> str:
    """The reply, then the newsletter in Markdown when the run presented or showed one."""
    newsletter = render_markdown(reply.newsletter) if reply.newsletter else None
    return "\n\n".join(part for part in (reply.text, newsletter) if part)


async def _caller(
    authorization: str | None, verify: Callable[[str], Awaitable[Caller]]
) -> Caller | None:
    """The caller of a request with a valid bearer token, or None."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not token:
        return None
    try:
        return await verify(token)
    except InvalidToken as error:
        # The reason says why without repeating the token, so a misconfigured issuer shows here.
        _log.info("token_rejected", extra={"reason": str(error)})
        return None


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse(_error_body(kind, message), status_code=status)


def _error_body(kind: str, message: str) -> dict[str, object]:
    """An OpenAI error response body."""
    error = ErrorObject(message=message, type=kind)
    return {"error": error.model_dump(mode="json", exclude_none=True)}


def _event(model: BaseModel) -> str:
    return f"data: {model.model_dump_json(exclude_none=True)}\n\n"
