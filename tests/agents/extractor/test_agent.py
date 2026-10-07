import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from pulse.agents.extractor.agent import ExtractorAgent
from pulse.entities.extracts import ExtractorOutput
from tests.emails import make_output, make_screened_email, share

pytestmark = pytest.mark.anyio

CATEGORIES = {"customer_win": "A new customer has signed.", "shout_out": "A colleague is thanked."}
EMAIL = make_screened_email("m01")
EXTRACTOR = ExtractorAgent(TestModel(), CATEGORIES)


def test_valid_output_has_no_problems() -> None:
    assert EXTRACTOR.checks(make_output(), EMAIL) == []


def test_excluded_output_may_have_no_category() -> None:
    output = make_output(category=None)

    assert EXTRACTOR.checks(output, EMAIL) == []


def test_category_and_reason_together_are_a_problem() -> None:
    problems = EXTRACTOR.checks(make_output(exclusion_reason="Not news."), EMAIL)

    assert problems == ["give either a category or an exclusion reason, not both"]


def test_neither_category_nor_reason_is_a_problem() -> None:
    problems = EXTRACTOR.checks(make_output(category=None, exclusion_reason=None), EMAIL)

    assert problems == ["give either a category or an exclusion reason"]


def test_unconfigured_category_is_a_problem() -> None:
    problems = EXTRACTOR.checks(make_output(category="gossip"), EMAIL)

    assert problems == ["category gossip is not a configured category"]


async def test_extractor_has_no_tools_and_lists_the_categories() -> None:
    seen: list[AgentInfo] = []
    prompts: list[ModelMessage] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        prompts.extend(messages)
        return ModelResponse(
            parts=[TextPart(share(make_output(), ExtractorOutput).model_dump_json())]
        )

    agent = ExtractorAgent(FunctionModel(model), CATEGORIES)

    assert await agent.answer(EMAIL) == share(make_output(), ExtractorOutput)
    [info] = seen
    assert (info.function_tools, info.output_tools) == ([], [])
    assert info.model_request_parameters.output_mode == "native"
    [request] = prompts
    assert isinstance(request, ModelRequest)
    assert request.instructions is not None
    assert "customer_win: A new customer has signed." in request.instructions
    assert [p.content for p in request.parts if isinstance(p, UserPromptPart)] == [
        agent.message(EMAIL)
    ]
