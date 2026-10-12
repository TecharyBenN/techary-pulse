"""Evaluates the judge on its own, on texts from a tested writer draft and three that each break
one rule, through the dev gateway, using ./config.yaml and .env. Every call is paid; the results,
with every request sent, go to evaluations/judge/."""

from typing import Any

import pytest
from pydantic_ai.models import Model

from pulse.agents.judge.agent import JudgeAgent
from pulse.config import Config
from pulse.entities.content import JudgeOutput
from tests.emails import corpus_expectation
from tests.evaluation import JudgeExpectation, judge_inputs, judge_problems, run_live

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SAMPLES = 5


async def test_the_judge_finds_exactly_the_unsupported_texts() -> None:
    expected = {
        case: JudgeExpectation.model_validate(answer)
        for case, answer in corpus_expectation("judge").items()
    }

    def build(model: Model, config: Config) -> JudgeAgent:
        return JudgeAgent(model)

    def problems(case: str, output: JudgeOutput) -> list[str]:
        return judge_problems(output, expected[case])

    answers = await run_live("judge", build, judge_inputs(), problems, _details, SAMPLES)

    assert all(not answer.problems for answer in answers)


def _details(output: dict[str, Any]) -> list[str]:
    return [f"unsupported: {c['claim']}" for c in output["claims"] if c["source"] is None]
