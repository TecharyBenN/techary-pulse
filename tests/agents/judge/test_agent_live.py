"""Evaluates the judge on its own, on texts from a tested writer draft and texts that each break
one rule, through the dev gateway, using ./config.yaml and .env. Every call is paid; the results,
with every request sent, go to evaluations/judge/."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.models import Model

from pulse.agents.judge.agent import JudgeAgent
from pulse.config import Config
from pulse.entities.content import JudgeOutput
from tests.emails import corpus_expectation
from tests.evaluation import (
    STORE_VARIABLE,
    WRITER_RESULTS_VARIABLE,
    JudgeExpectation,
    judge_inputs,
    judge_problems,
    run_live,
    saved_judge_inputs,
    saved_run,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SAMPLES = 5


@pytest.mark.skipif(STORE_VARIABLE in os.environ, reason="a saved run's store is given")
async def test_the_judge_finds_exactly_the_unsupported_texts() -> None:
    expected = {
        case: JudgeExpectation.model_validate(answer)
        for case, answer in corpus_expectation("judge").items()
    }

    def build(models: Callable[[str], Model], config: Config) -> JudgeAgent:
        return JudgeAgent(models("judge"))

    def problems(case: str, output: JudgeOutput) -> list[str]:
        return judge_problems(output, expected[case])

    answers = await run_live("judge", build, judge_inputs(), problems, _details, SAMPLES)

    assert all(not answer.problems for answer in answers)


@pytest.mark.skipif(STORE_VARIABLE not in os.environ, reason="needs a saved run's store")
async def test_the_judge_rejudges_a_saved_run() -> None:
    """The saved run has no expected answer: every finding is read against the item's facts.
    Version 1's texts are judged `SAMPLES` times; each writer draft once."""
    run = await saved_run(Path(os.environ[STORE_VARIABLE]))
    drafts = os.environ.get(WRITER_RESULTS_VARIABLE)
    inputs = saved_judge_inputs(run, Path(drafts) if drafts else None)

    def build(models: Callable[[str], Model], config: Config) -> JudgeAgent:
        return JudgeAgent(models("judge"))

    await run_live("judge", build, inputs, lambda _, __: [], _details, 1 if drafts else SAMPLES)


def _details(output: dict[str, Any]) -> list[str]:
    return [f"unsupported: {part['text']} ({part['reason']})" for part in output["unsupported"]]
