"""Stand-in models for the orchestrator, and a test-only tool."""

from collections.abc import Awaitable, Callable
from datetime import timedelta

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.orchestrator.agent import INSTRUCTIONS
from pulse.agents.orchestrator.run import Orchestrator
from pulse.entities.store import Store

ModelFunction = Callable[[list[ModelMessage], AgentInfo], Awaitable[ModelResponse]]


class Tools:
    """A test-only tool, because the orchestrator has none until phase 4."""

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
