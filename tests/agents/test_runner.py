import pytest
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.runner import run_specialist
from pulse.entities.errors import SpecialistFailed
from tests.fakes.models import gateway_error

pytestmark = pytest.mark.anyio


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


def _no_problems(output: _Output) -> list[str]:
    return []


def _positive(output: _Output) -> list[str]:
    return [] if output.count > 0 else ["count must be positive"]


def _agent(
    *answers: ModelResponse | Exception,
) -> tuple[Agent[None, _Output], list[list[ModelMessage]]]:
    remaining = list(answers)
    calls: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(list(messages))
        answer = remaining.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return Agent(FunctionModel(model), output_type=NativeOutput(_Output), name="counter"), calls


def _json(text: str, finish_reason: str | None = None) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)], finish_reason=finish_reason)  # type: ignore[arg-type]


async def test_returns_a_valid_response() -> None:
    agent, calls = _agent(_json('{"count": 3}'))

    assert await run_specialist(agent, "Count", _no_problems) == _Output(count=3)
    assert len(calls) == 1


def _prompt(request: ModelMessage) -> str:
    assert isinstance(request, ModelRequest)
    [prompt] = [p.content for p in request.parts if isinstance(p, UserPromptPart)]
    assert isinstance(prompt, str)
    return prompt


@pytest.mark.parametrize(
    ("invalid", "problem"),
    [
        (_json('{"count": 3, "extra": 1}'), "the response did not match the output type"),
        (_json("not json"), "the response did not match the output type"),
        (_json('{"count": 3}', "length"), "the response was truncated or refused"),
        (_json('{"count": 3}', "content_filter"), "the response was truncated or refused"),
        (_json('{"count": 0}'), "count must be positive"),
    ],
)
async def test_retry_sends_the_problems_back_with_the_invalid_response(
    invalid: ModelResponse, problem: str
) -> None:
    agent, calls = _agent(invalid, _json('{"count": 4}'))

    assert await run_specialist(agent, "Count", _positive) == _Output(count=4)
    first, retry = calls
    assert len(first) == 1
    [request, response, correction] = retry
    assert _prompt(request) == "Count"
    assert response.parts == invalid.parts
    assert f"- {problem}" in _prompt(correction)


async def test_fails_after_two_invalid_responses() -> None:
    agent, _ = _agent(_json('{"count": 0}'), _json('{"count": -1}'))

    with pytest.raises(SpecialistFailed, match=r"counter.*count must be positive"):
        await run_specialist(agent, "Count", _positive)


async def test_gateway_error_is_not_retried() -> None:
    agent, calls = _agent(gateway_error())

    with pytest.raises(Exception, match="502"):
        await run_specialist(agent, "Count", _no_problems)
    assert len(calls) == 1
