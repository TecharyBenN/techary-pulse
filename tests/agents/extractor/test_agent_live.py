"""Evaluates the extractor on its own on the corpus and the made-up dense notices, through the
dev gateway, using ./config.yaml and .env. Every call is paid; the results, with every request
sent, go to evaluations/extractor/."""

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.models import Model

from pulse.agents.extractor.agent import ExtractorAgent
from pulse.config import Config
from pulse.entities.extracts import ExtractorOutput, extracted_news
from tests.evaluation import (
    STORE_VARIABLE,
    ExtractorExpectation,
    corpus_inputs,
    dense_notice_expectations,
    dense_notice_inputs,
    expectations,
    extractor_problems,
    run_live,
    saved_email_inputs,
    saved_run,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SAMPLES = 5


@pytest.mark.skipif(STORE_VARIABLE in os.environ, reason="a saved run's store is given")
async def test_the_extractor_gives_the_expected_answer_for_every_corpus_email() -> None:
    expected = expectations("extractor", ExtractorExpectation) | dense_notice_expectations()
    inputs = [*corpus_inputs(), *dense_notice_inputs()]

    def problems(corpus_id: str, output: ExtractorOutput) -> list[str]:
        return extractor_problems(extracted_news(output), expected[corpus_id])

    answers = await run_live("extractor", _build, inputs, problems, _details, SAMPLES)

    assert all(not answer.problems for answer in answers)


@pytest.mark.skipif(STORE_VARIABLE not in os.environ, reason="needs a saved run's store")
async def test_the_extractor_reads_a_saved_run_emails() -> None:
    """The saved run has no expected answer: the facts are read against the email."""
    run = await saved_run(Path(os.environ[STORE_VARIABLE]))

    await run_live(
        "extractor", _build, saved_email_inputs(run), lambda _, __: [], _details, SAMPLES
    )


def _build(model: Model, config: Config) -> ExtractorAgent:
    return ExtractorAgent(model, config.categories)


def _details(output: dict[str, Any]) -> list[str]:
    return [f"summary: {output['summary']}", f"exclusion_reason: {output['exclusion_reason']}"]
