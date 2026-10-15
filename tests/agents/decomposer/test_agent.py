import asyncio
import json
import re
from pathlib import Path

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from pulse.agents import decomposer
from pulse.agents.decomposer.agent import DecomposerAgent, DecomposerTask
from pulse.entities.extracts import DecomposerOutput, ExtractorOutput, LineFacts
from pulse.entities.mail import message_lines
from tests.emails import make_screened_email
from tests.fakes.models import gateway_error

pytestmark = pytest.mark.anyio

FACT = "A 15 per cent surcharge applies from 3 October 2026, except for schools (1 December 2026)."
SPLIT = [
    "A 15 per cent surcharge applies from 3 October 2026.",
    "For schools, it applies from 1 December 2026.",
]
TASK = DecomposerTask(
    line="- Surcharge: 15%, from 3 October 2026, schools 1 December 2026.", facts=[FACT]
)
DECOMPOSER = DecomposerAgent(TestModel())


def test_the_message_holds_only_the_facts() -> None:
    assert DECOMPOSER.message(TASK) == f"<facts>\n{json.dumps([FACT])}\n</facts>"


def test_split_facts_keeping_every_number_have_no_problems() -> None:
    assert DECOMPOSER.checks(DecomposerOutput(facts=SPLIT), TASK) == []


def test_a_number_dropped_or_added_is_a_problem() -> None:
    dropped = DecomposerOutput(facts=["A surcharge applies from 3 October 2026.", SPLIT[1]])
    added = DecomposerOutput(facts=[*SPLIT, "It ends on 31 March 2027."])

    assert DECOMPOSER.checks(dropped, TASK) == ["dropped 15"]
    assert DECOMPOSER.checks(added, TASK) == ["added 31", "added 2027"]


async def test_split_sends_each_line_with_facts_and_replaces_its_facts() -> None:
    email = make_screened_email(
        subject="Prices",
        body=f"Dear partner,\n{TASK.line}\nThanks",
    )
    sent: list[str] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[0]
        assert isinstance(request, ModelRequest)
        sent.extend(
            p.content
            for p in request.parts
            if isinstance(p, UserPromptPart) and isinstance(p.content, str)
        )
        return ModelResponse(parts=[TextPart(DecomposerOutput(facts=SPLIT).model_dump_json())])

    output = ExtractorOutput(
        category="company_notices",
        exclusion_reason=None,
        summary="Prices change.",
        people=[],
        lines=[
            LineFacts(line=n, facts=[FACT] if n == 2 else [])
            for n in range(len(message_lines(email)))
        ],
    )

    split = await DecomposerAgent(FunctionModel(model)).split(output, email)

    assert sent == [DECOMPOSER.message(DecomposerTask(line="", facts=[FACT]))]
    assert [line.facts for line in split.lines] == [[], [], SPLIT, []]
    assert split.model_dump(exclude={"lines"}) == output.model_dump(exclude={"lines"})


def test_every_prompt_example_keeps_its_numbers() -> None:
    prompt = (Path(decomposer.__file__).parent / "prompt.md").read_text(encoding="utf-8")
    examples = re.findall(r"<facts>\n(.*?)\n</facts>\n<output>\n(.*?)\n</output>", prompt, re.S)

    assert 3 <= len(examples) <= 5
    for facts_json, output_json in examples:
        task = DecomposerTask(line="", facts=json.loads(facts_json))
        output = DecomposerOutput.model_validate_json(output_json)
        assert DECOMPOSER.checks(output, task) == []


async def test_a_gateway_error_fails_split_once_every_other_call_has_finished() -> None:
    email = make_screened_email(subject="Prices", body="Line one.\nLine two.")
    finished: list[str] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "first" in str(messages[0]):
            raise gateway_error()
        await asyncio.sleep(0.05)
        finished.append("second")
        return ModelResponse(
            parts=[TextPart(DecomposerOutput(facts=["The second fact."]).model_dump_json())]
        )

    output = ExtractorOutput(
        category="company_notices",
        exclusion_reason=None,
        summary="Two lines.",
        people=[],
        lines=[
            LineFacts(line=0, facts=[]),
            LineFacts(line=1, facts=["The first fact."]),
            LineFacts(line=2, facts=["The second fact."]),
        ],
    )

    with pytest.raises(ModelHTTPError):
        await DecomposerAgent(FunctionModel(model)).split(output, email)

    assert finished == ["second"]
