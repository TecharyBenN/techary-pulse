"""Stand-in models for the orchestrator and the extractor, and a test-only tool."""

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta

from pydantic_ai import Agent
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
from pulse.entities.store import Store

ModelFunction = Callable[[list[ModelMessage], AgentInfo], Awaitable[ModelResponse]]


class Tools:
    """A test-only tool, so run tests do not depend on the orchestrator's real tools."""

    def __init__(self) -> None:
        self.calls = 0

    def ping(self) -> str:
        self.calls += 1
        return "pong"


def make_orchestrator(
    store: Store,
    model: ModelFunction,
    tools: Tools | None = None,
    max_tool_calls: int = 40,
    max_run_time: timedelta = timedelta(minutes=15),
) -> Orchestrator:
    agent = Agent(
        FunctionModel(model),
        output_type=str,
        instructions=INSTRUCTIONS,
        tools=[tools.ping] if tools else [],
    )
    return Orchestrator(agent, store, max_tool_calls, max_run_time)


def text_response(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def ping_call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("ping", {})])


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
    """Answer each submission with the JSON set for its message ID, found in the prompt."""

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        [request] = messages
        assert isinstance(request, ModelRequest)
        [prompt] = [p.content for p in request.parts if isinstance(p, UserPromptPart)]
        assert isinstance(prompt, str)
        submission = json.loads(prompt.split("\n")[1])
        return text_response(outputs[submission["message_id"]])

    return FunctionModel(model)
