"""Evaluates the extractor on its own on the corpus, through the dev gateway, using ./config.yaml
and .env. Every call is paid; the results, with every request sent, go to evaluations/extractor/."""

from typing import Any

import pytest
from pydantic_ai.models import Model

from pulse.agents.extractor.agent import ExtractorAgent
from pulse.config import Config
from pulse.entities.extracts import ExtractorOutput
from tests.evaluation import ExtractorExpectation, expectations, extractor_problems, run_live

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SAMPLES = 5


async def test_the_extractor_gives_the_expected_answer_for_every_corpus_email() -> None:
    expected = expectations("extractor", ExtractorExpectation)

    def build(model: Model, config: Config) -> ExtractorAgent:
        return ExtractorAgent(model, config.categories)

    def problems(corpus_id: str, output: ExtractorOutput) -> list[str]:
        return extractor_problems(output, expected[corpus_id])

    answers = await run_live("extractor", build, problems, _details, SAMPLES)

    assert all(not answer.problems for answer in answers)


def _details(output: dict[str, Any]) -> list[str]:
    return [f"summary: {output['summary']}", f"exclusion_reason: {output['exclusion_reason']}"]
