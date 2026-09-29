"""The LibreChat endpoint: OpenAI chat completions over HTTP, in front of the orchestrator."""

import asyncio
import hmac
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from pulse.agents.orchestrator.run import Orchestrator
from pulse.entities.clock import Clock
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import PulseError
from pulse.entities.mail import address_in

PATH = "/v1/chat/completions"
NOT_A_REVIEWER = "You are not a Pulse reviewer, so Pulse has not acted on your message."
_RUN_FAILED = "Pulse could not complete your request. Please try again."


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


class _AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class _Choice(BaseModel):
    index: int = 0
    message: _AssistantMessage
    finish_reason: Literal["stop"] = "stop"


class _ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[_Choice]


class _Delta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None


class _ChunkChoice(BaseModel):
    index: int = 0
    delta: _Delta
    finish_reason: Literal["stop"] | None = None


class _ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[_ChunkChoice]


class _ErrorDetail(BaseModel):
    message: str
    type: str


class _ErrorBody(BaseModel):
    error: _ErrorDetail


class _Completion:
    """The identity every part of one response shares."""

    def __init__(self, model: str, clock: Clock) -> None:
        self.id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(clock.now().timestamp())
        self.model = model

    def whole(self, content: str) -> _ChatCompletion:
        choice = _Choice(message=_AssistantMessage(content=content))
        return _ChatCompletion(id=self.id, created=self.created, model=self.model, choices=[choice])

    def chunk(self, delta: _Delta, finish_reason: Literal["stop"] | None = None) -> str:
        choice = _ChunkChoice(delta=delta, finish_reason=finish_reason)
        chunk = _ChatCompletionChunk(
            id=self.id, created=self.created, model=self.model, choices=[choice]
        )
        return _event(chunk)


def create_app(
    orchestrator: Orchestrator, reviewers: Sequence[str], gateway_key: str, clock: Clock
) -> FastAPI:
    app = FastAPI()
    # Runs outlive their requests, so a client that disconnects never cancels a run.
    running: set[asyncio.Task[str | None]] = set()

    @app.post(PATH)
    async def chat_completions(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_user_email: Annotated[str | None, Header()] = None,
    ) -> Response:
        if not _authorised(authorization, gateway_key):
            return _error(401, "invalid_request_error", "The gateway credential is not valid.")
        try:
            chat = _ChatRequest.model_validate_json(await request.body())
        except ValidationError:
            return _error(400, "invalid_request_error", "The request is not a chat completion.")
        user_messages = [message for message in chat.messages if message.role == "user"]
        if not user_messages:
            return _error(400, "invalid_request_error", "The request has no user message.")

        notes: asyncio.Queue[str | None] = asyncio.Queue()
        if x_user_email is not None and address_in(x_user_email, reviewers):
            message = ReviewerMessage(
                message_id=str(uuid.uuid4()),
                author=x_user_email,
                channel="librechat",
                text=user_messages[-1].text(),
                received=clock.now(),
            )
            task = asyncio.create_task(orchestrator.handle(message, notes.put))
        else:
            task = asyncio.create_task(_fixed(NOT_A_REVIEWER))
        running.add(task)

        def finished(task: asyncio.Task[str | None]) -> None:
            running.discard(task)
            notes.put_nowait(None)
            # The run logs its own failure; retrieving it here stops asyncio logging it again.
            if not task.cancelled():
                task.exception()

        task.add_done_callback(finished)

        completion = _Completion(chat.model, clock)
        if chat.stream:
            return StreamingResponse(
                _stream(task, notes, completion), media_type="text/event-stream"
            )
        try:
            reply = await asyncio.shield(task)
        except PulseError:
            return _error(500, "server_error", _RUN_FAILED)
        return JSONResponse(completion.whole(reply or "").model_dump(mode="json"))

    return app


async def serve(app: FastAPI, port: int) -> None:
    """Serve the app on Pulse's event loop, logging through Pulse's JSON log format."""
    # In a container, the gateway's connections arrive on the container's own interface.
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None)
    await uvicorn.Server(config).serve()


async def _fixed(reply: str) -> str | None:
    return reply


async def _stream(
    task: asyncio.Task[str | None], notes: asyncio.Queue[str | None], completion: _Completion
) -> AsyncIterator[str]:
    yield completion.chunk(_Delta(role="assistant"))
    while (note := await notes.get()) is not None:
        yield completion.chunk(_Delta(content=f"{note}\n\n"))
    try:
        reply = task.result()
    except PulseError:
        error = _ErrorBody(error=_ErrorDetail(message=_RUN_FAILED, type="server_error"))
        yield _event(error)
    else:
        if reply:
            yield completion.chunk(_Delta(content=reply))
        yield completion.chunk(_Delta(), finish_reason="stop")
    yield "data: [DONE]\n\n"


def _authorised(authorization: str | None, gateway_key: str) -> bool:
    scheme, _, token = (authorization or "").partition(" ")
    return scheme.casefold() == "bearer" and hmac.compare_digest(
        token.encode(), gateway_key.encode()
    )


def _error(status: int, kind: str, message: str) -> JSONResponse:
    body = _ErrorBody(error=_ErrorDetail(message=message, type=kind))
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def _event(model: BaseModel) -> str:
    return f"data: {model.model_dump_json(exclude_none=True)}\n\n"
