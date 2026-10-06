"""Stand-in models for the orchestrator and the extractor, and a test-only tool."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta

from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.orchestrator.agent import INSTRUCTIONS
from pulse.agents.orchestrator.run import Orchestrator
from pulse.agents.orchestrator.tools import ShowResult
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.store import Store
from pulse.services.operations import PresentResult

ModelFunction = Callable[[list[ModelMessage], AgentInfo], Awaitable[ModelResponse]]


class Tools:
    """Test-only tools, so run tests do not depend on the orchestrator's real tools."""

    def __init__(self) -> None:
        self.calls = 0
        # The message ID each run passed to its tools, in call order.
        self.callers: list[str] = []

    def ping(self) -> str:
        self.calls += 1
        return "pong"

    def caller(self, ctx: RunContext[ReviewerMessage]) -> str:
        self.callers.append(ctx.deps.message_id)
        return "noted"

    def present_draft(self) -> PresentResult:
        """Stands in for the real tool, presenting version 1, which the test stores."""
        return PresentResult(version=1, flagged=0)

    def show_draft(self) -> ShowResult:
        """Stands in for the real tool, showing the working draft, which the test stores."""
        return ShowResult(version=None)


def make_orchestrator(
    store: Store,
    model: ModelFunction,
    tools: Tools | None = None,
    max_tool_calls: int = 40,
    max_run_time: timedelta = timedelta(minutes=15),
    lock: asyncio.Lock | None = None,
) -> Orchestrator:
    agent = Agent(
        FunctionModel(model),
        deps_type=ReviewerMessage,
        output_type=str,
        instructions=INSTRUCTIONS,
        tools=[tools.ping, tools.caller, tools.present_draft, tools.show_draft] if tools else [],
    )
    return Orchestrator(agent, store, max_tool_calls, max_run_time, lock or asyncio.Lock())


def text_response(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def ping_call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("ping", {})])


def caller_call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("caller", {})])


def present_call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("present_draft", {})])


def show_call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("show_draft", {})])


def gateway_error() -> ModelHTTPError:
    return ModelHTTPError(status_code=502, model_name="stand-in")


def reply_with(text: str) -> ModelFunction:
    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return text_response(text)

    return model


def responses(*answers: ModelResponse | Exception) -> ModelFunction:
    """Answer each model request with the next response, or raise the next exception."""
    remaining = list(answers)

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answer = remaining.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return model


def extractor_model(outputs: Mapping[str, str]) -> FunctionModel:
    """Answer each email with the JSON set for its body, found in the first prompt, so a retry
    gets the same answer."""

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[0]
        assert isinstance(request, ModelRequest)
        [prompt] = [p.content for p in request.parts if isinstance(p, UserPromptPart)]
        assert isinstance(prompt, str)
        email = json.loads(prompt.split("\n")[1])
        return text_response(outputs[email["body"]])

    return FunctionModel(model)
