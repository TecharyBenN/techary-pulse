import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.sensitivity.agent import SensitivityAgent
from pulse.entities.extracts import SensitivityOutput
from tests.emails import make_output, make_screened_email, share

pytestmark = pytest.mark.anyio

BODY = "Northwind Retail signed on 22 September."
EMAIL = make_screened_email("m01", body=BODY)


async def test_sensitivity_agent_has_no_tools_and_keeps_the_email_out_of_its_instructions() -> None:
    seen: list[AgentInfo] = []
    prompts: list[ModelMessage] = []
    output = share(make_output(), SensitivityOutput)

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        prompts.extend(messages)
        return ModelResponse(parts=[TextPart(output.model_dump_json())])

    agent = SensitivityAgent(FunctionModel(model))

    assert await agent.answer(EMAIL) == output
    [info] = seen
    assert (info.function_tools, info.output_tools) == ([], [])
    assert info.model_request_parameters.output_mode == "native"
    [request] = prompts
    assert isinstance(request, ModelRequest)
    assert request.instructions is not None
    assert BODY not in request.instructions
    assert [p.content for p in request.parts if isinstance(p, UserPromptPart)] == [
        agent.message(EMAIL)
    ]
