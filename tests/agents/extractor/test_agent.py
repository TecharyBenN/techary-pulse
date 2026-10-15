import json
import re
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from pulse.agents import extractor
from pulse.agents.extractor.agent import ExtractorAgent
from pulse.entities.extracts import ExtractorOutput, LineFacts
from pulse.entities.mail import message_lines
from tests.emails import answer_for, make_output, make_screened_email

pytestmark = pytest.mark.anyio

CATEGORIES = {"customer_win": "A new customer has signed.", "shout_out": "A colleague is thanked."}
EMAIL = make_screened_email(
    "m01",
    subject="Signed today",
    body="Hi all,\n\nWe signed Northwind Retail on 22 September.\n\nPriya",
)
EXTRACTOR = ExtractorAgent(TestModel(), CATEGORIES)


def _answer(**changes: object) -> ExtractorOutput:
    return answer_for(make_output(**changes), message_lines(EMAIL))


def _lines(*numbers: int) -> ExtractorOutput:
    return _answer().model_copy(update={"lines": [LineFacts(line=n, facts=[]) for n in numbers]})


def test_valid_output_has_no_problems() -> None:
    assert EXTRACTOR.checks(_answer(), EMAIL) == []


def test_excluded_output_may_have_no_category() -> None:
    assert EXTRACTOR.checks(_answer(category=None), EMAIL) == []


def test_category_and_reason_together_are_a_problem() -> None:
    problems = EXTRACTOR.checks(_answer(exclusion_reason="Not news."), EMAIL)

    assert problems == ["give either a category or an exclusion reason, not both"]


def test_neither_category_nor_reason_is_a_problem() -> None:
    problems = EXTRACTOR.checks(_answer(category=None, exclusion_reason=None), EMAIL)

    assert problems == ["give either a category or an exclusion reason"]


def test_unconfigured_category_is_a_problem() -> None:
    problems = EXTRACTOR.checks(_answer(category="gossip"), EMAIL)

    assert problems == ["category gossip is not a configured category"]


async def test_extractor_has_no_tools_and_lists_the_categories() -> None:
    seen: list[AgentInfo] = []
    prompts: list[ModelMessage] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        prompts.extend(messages)
        return ModelResponse(parts=[TextPart(_answer().model_dump_json())])

    agent = ExtractorAgent(FunctionModel(model), CATEGORIES)

    assert await agent.answer(EMAIL) == _answer()
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
    # Each field says what it holds in the schema the model is given.
    schema = info.model_request_parameters.output_object
    assert schema is not None
    assert all(field.get("description") for field in schema.json_schema["properties"].values())


def test_the_message_holds_the_email_without_its_body_and_its_numbered_lines() -> None:
    message = EXTRACTOR.message(EMAIL)

    email, lines = message.split("\n\n")
    assert '"body"' not in email and '"unique_body"' in email and '"subject"' in email
    assert lines == (
        "<lines>\n"
        '[{"line": 0, "text": "Signed today"}, {"line": 1, "text": "Hi all,"}, '
        '{"line": 2, "text": "We signed Northwind Retail on 22 September."}, '
        '{"line": 3, "text": "Priya"}]\n'
        "</lines>"
    )


def test_a_forward_gives_its_new_text_and_numbers_every_line_it_forwards() -> None:
    forward = make_screened_email(
        subject="FW: Prices",
        unique_body="For anyone ordering laptops.",
        body="For anyone ordering laptops.\n\nFrom: Litware Supplies\nPrices rise on 1 November.",
    )

    email, lines = EXTRACTOR.message(forward).split("\n\n")

    assert json.loads(email.split("\n")[1])["unique_body"] == "For anyone ordering laptops."
    assert [line["text"] for line in json.loads(lines.split("\n")[1])] == [
        "FW: Prices",
        "For anyone ordering laptops.",
        "From: Litware Supplies",
        "Prices rise on 1 November.",
    ]


@pytest.mark.parametrize(
    ("numbers", "problem"),
    [
        ((0, 1, 3), "missing 2"),
        ((0, 1, 2, 2, 3), "repeated 2"),
        ((0, 1, 2, 3, 7), "not in <lines> 7"),
        ((1, 3, 3, 9), "missing 0, 2; repeated 3; not in <lines> 9"),
    ],
)
def test_every_line_must_be_listed_once(numbers: tuple[int, ...], problem: str) -> None:
    assert EXTRACTOR.checks(_lines(*numbers), EMAIL) == [
        f"list every line in <lines> once in lines: {problem}"
    ]


def test_every_problem_is_listed_so_the_retry_can_fix_them_all() -> None:
    answer = answer_for(
        make_output(category="gossip", facts=["We signed Northwind Retail on 1 May."]),
        message_lines(EMAIL),
    )
    problems = EXTRACTOR.checks(answer.model_copy(update={"lines": answer.lines[:-1]}), EMAIL)

    assert problems == [
        "list every line in <lines> once in lines: missing 3",
        "category gossip is not a configured category",
        "write each number only as the email gives it, adding none: added 1",
    ]


def test_every_prompt_example_answers_line_by_line_for_its_email() -> None:
    prompt = (Path(extractor.__file__).parent / "prompt.md").read_text(encoding="utf-8")
    examples = re.findall(
        r"<email>\n(.*?)\n</email>\n<lines>\n(.*?)\n</lines>\n<output>\n(.*?)\n</output>",
        prompt,
        re.S,
    )
    agent = ExtractorAgent(
        TestModel(), {c: c for c in ("customer_win", "delivery_highlight", "company_notices")}
    )

    assert len(examples) == 5
    for email_json, lines_json, output_json in examples:
        lines = [line["text"] for line in json.loads(lines_json)]
        email = make_screened_email(**json.loads(email_json), body="\n".join(lines[1:]))
        output = ExtractorOutput.model_validate_json(output_json)
        # The example shows the message the extractor really gets, and a valid answer to it.
        assert agent.message(email) == (
            "<email>\n" + email_json + "\n</email>\n\n<lines>\n" + lines_json + "\n</lines>"
        )
        assert agent.checks(output, email) == []


def test_a_number_the_email_does_not_give_is_a_problem() -> None:
    lines = message_lines(EMAIL)
    added = answer_for(make_output(facts=["We signed Northwind Retail on 1 May 2026."]), lines)
    given = answer_for(make_output(facts=["We signed Northwind Retail."]), lines)

    assert EXTRACTOR.checks(added, EMAIL) == [
        "write each number only as the email gives it, adding none: added 1, added 2026"
    ]
    assert EXTRACTOR.checks(given, EMAIL) == []
