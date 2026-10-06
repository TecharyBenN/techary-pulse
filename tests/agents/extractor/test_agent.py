import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.extractor.agent import build_extractor, output_checks
from pulse.entities.extracts import ExtractorOutput
from tests.emails import make_output, share

pytestmark = pytest.mark.anyio

CATEGORIES = {"customer_win": "A new customer has signed.", "shout_out": "A colleague is thanked."}


def test_valid_output_has_no_problems() -> None:
    assert output_checks(make_output(), CATEGORIES) == []


def test_excluded_output_may_have_no_category() -> None:
    output = make_output(category=None)

    assert output_checks(output, CATEGORIES) == []


def test_category_and_reason_together_are_a_problem() -> None:
    problems = output_checks(make_output(exclusion_reason="Not news."), CATEGORIES)

    assert problems == ["give either a category or an exclusion reason, not both"]


def test_neither_category_nor_reason_is_a_problem() -> None:
    problems = output_checks(make_output(category=None, exclusion_reason=None), CATEGORIES)

    assert problems == ["give either a category or an exclusion reason"]


def test_unconfigured_category_is_a_problem() -> None:
    problems = output_checks(make_output(category="gossip"), CATEGORIES)

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

    result = await build_extractor(FunctionModel(model), CATEGORIES).run("Extract")

    assert result.output == share(make_output(), ExtractorOutput)
    [info] = seen
    assert (info.function_tools, info.output_tools) == ([], [])
    assert info.model_request_parameters.output_mode == "native"
    [request] = prompts
    assert isinstance(request, ModelRequest)
    assert request.instructions is not None
    assert "customer_win: A new customer has signed." in request.instructions
    assert [p.content for p in request.parts if isinstance(p, UserPromptPart)] == ["Extract"]
