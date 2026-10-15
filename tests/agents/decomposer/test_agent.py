import json
import re
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from pulse.agents import decomposer
from pulse.agents.decomposer.agent import DecomposerAgent, DecomposerTask
from pulse.entities.extracts import DecomposerOutput

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


def test_every_prompt_example_keeps_its_numbers() -> None:
    prompt = (Path(decomposer.__file__).parent / "prompt.md").read_text(encoding="utf-8")
    examples = re.findall(r"<facts>\n(.*?)\n</facts>\n<output>\n(.*?)\n</output>", prompt, re.S)

    assert 3 <= len(examples) <= 5
    for facts_json, output_json in examples:
        task = DecomposerTask(line="", facts=json.loads(facts_json))
        output = DecomposerOutput.model_validate_json(output_json)
        assert DECOMPOSER.checks(output, task) == []
