import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.sensitivity.agent import build_sensitivity
from pulse.entities.extracts import SensitivityOutput
from tests.emails import make_output, share

pytestmark = pytest.mark.anyio

PROMPT = "Northwind Retail signed on 22 September."


async def test_sensitivity_agent_has_no_tools_and_keeps_the_email_out_of_its_instructions() -> None:
    seen: list[AgentInfo] = []
    prompts: list[ModelMessage] = []
    output = share(make_output(), SensitivityOutput)

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        prompts.extend(messages)
        return ModelResponse(parts=[TextPart(output.model_dump_json())])

    result = await build_sensitivity(FunctionModel(model)).run(PROMPT)

    assert result.output == output
    [info] = seen
    assert (info.function_tools, info.output_tools) == ([], [])
    assert info.model_request_parameters.output_mode == "native"
    [request] = prompts
    assert isinstance(request, ModelRequest)
    assert request.instructions is not None
    assert PROMPT not in request.instructions
    assert [p.content for p in request.parts if isinstance(p, UserPromptPart)] == [PROMPT]
