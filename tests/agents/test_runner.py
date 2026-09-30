import pytest
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
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


def _agent(*answers: ModelResponse | Exception) -> tuple[Agent[None, _Output], list[int]]:
    remaining = list(answers)
    calls: list[int] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(len(messages))
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


@pytest.mark.parametrize(
    "invalid",
    [
        _json('{"count": 3, "extra": 1}'),
        _json("not json"),
        _json('{"count": 3}', "length"),
        _json('{"count": 3}', "content_filter"),
        _json('{"count": 0}'),
    ],
)
async def test_retries_an_invalid_response_once_afresh(invalid: ModelResponse) -> None:
    agent, calls = _agent(invalid, _json('{"count": 4}'))

    assert await run_specialist(agent, "Count", _positive) == _Output(count=4)
    # The retry starts afresh, without the invalid response in its history.
    assert calls == [1, 1]


async def test_fails_after_two_invalid_responses() -> None:
    agent, _ = _agent(_json('{"count": 0}'), _json('{"count": -1}'))

    with pytest.raises(SpecialistFailed, match=r"counter.*count must be positive"):
        await run_specialist(agent, "Count", _positive)


async def test_gateway_error_is_not_retried() -> None:
    agent, calls = _agent(gateway_error())

    with pytest.raises(Exception, match="502"):
        await run_specialist(agent, "Count", _no_problems)
    assert len(calls) == 1
