"""Evaluates the sensitivity agent on its own on the corpus, through the dev gateway, using
./config.yaml and .env. Every call is paid; the results, with every request sent, go to
evaluations/sensitivity/."""

from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai.models import Model

from pulse.agents.sensitivity.agent import SensitivityAgent
from pulse.config import Config
from pulse.entities.extracts import SensitivityOutput
from tests.evaluation import (
    SensitivityExpectation,
    corpus_inputs,
    expectations,
    run_live,
    sensitivity_problems,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SAMPLES = 5


async def test_the_sensitivity_agent_gives_the_expected_flags_for_every_corpus_email() -> None:
    expected = expectations("sensitivity", SensitivityExpectation)

    def build(models: Callable[[str], Model], config: Config) -> SensitivityAgent:
        return SensitivityAgent(models("sensitivity"))

    def problems(corpus_id: str, output: SensitivityOutput) -> list[str]:
        return sensitivity_problems(output, expected[corpus_id])

    answers = await run_live("sensitivity", build, corpus_inputs(), problems, _details, SAMPLES)

    assert all(not answer.problems for answer in answers)


def _details(output: dict[str, Any]) -> list[str]:
    flags = output["sensitivity"]
    if not flags:
        return ["no flags"]
    return [
        f"{flag['kind']} {'withheld' if flag['withheld'] else 'not withheld'}: {flag['evidence']}"
        for flag in flags
    ]
