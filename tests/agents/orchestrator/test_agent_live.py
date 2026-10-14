"""Evaluates the orchestrator on its own, with every specialist agent replaced by a stand-in
answering with its baseline, through the dev gateway, using ./config.yaml and .env. Every call is
paid; the results, with every request sent, go to evaluations/orchestrator/."""

from typing import Any

import pytest

from tests.evaluation import (
    OrchestratorScenarios,
    Turn,
    orchestrator_expectations,
    orchestrator_inputs,
    orchestrator_problems,
    run_live,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SAMPLES = 3


async def test_the_orchestrator_handles_every_scenario_as_expected() -> None:
    expected = orchestrator_expectations()

    def problems(scenario: str, turn: Turn) -> list[str]:
        return orchestrator_problems(turn, expected[scenario])

    answers = await run_live(
        "orchestrator",
        lambda models, config: OrchestratorScenarios(models("orchestrator"), config),
        orchestrator_inputs(),
        problems,
        _details,
        SAMPLES,
    )

    assert all(not answer.problems for answer in answers)


def _details(output: dict[str, Any]) -> list[str]:
    return [f"tools: {output['tools']}", f"reply: {output['reply']}"]
