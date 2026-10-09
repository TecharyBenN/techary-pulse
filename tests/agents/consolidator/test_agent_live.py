"""Evaluates the consolidator on its own, on the included records the first-stage baselines
produced, with and without three restored records, through the dev gateway, using ./config.yaml
and .env. Every call is paid; the results, with every request sent, go to
evaluations/consolidator/."""

from typing import Any

import pytest
from pydantic_ai.models import Model

from pulse.agents.consolidator.agent import ConsolidatorAgent
from pulse.config import Config
from pulse.entities.extracts import ConsolidatorOutput, numbered_records
from tests.evaluation import (
    consolidator_expectations,
    consolidator_inputs,
    consolidator_problems,
    run_live,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

# One call per input and sample, as the consolidator takes every included record at once.
SAMPLES = 10


@pytest.mark.parametrize("input_id", ["included", "restored"])
async def test_the_consolidator_merges_the_records_as_expected(input_id: str) -> None:
    expected = consolidator_expectations()
    inputs = [item for item in consolidator_inputs() if item.input_id == input_id]
    sources = {
        item.input_id: {
            record_id: record.message_id
            for record_id, record in numbered_records(item.task.records).items()
        }
        for item in inputs
    }

    def build(model: Model, config: Config) -> ConsolidatorAgent:
        return ConsolidatorAgent(model, config.categories)

    def problems(input_id: str, output: ConsolidatorOutput) -> list[str]:
        return consolidator_problems(output, sources[input_id], expected[input_id])

    answers = await run_live("consolidator", build, inputs, problems, _details, SAMPLES)

    assert all(not answer.problems for answer in answers)


def _details(output: dict[str, Any]) -> list[str]:
    return [f"headline: {output['headline']}", f"{len(output['items'])} items"]
