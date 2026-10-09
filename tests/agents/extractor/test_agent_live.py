"""Evaluates the extractor on its own on the corpus, through the dev gateway, using ./config.yaml
and .env. Every call is paid; the results, with every request sent, go to evaluations/extractor/."""

import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pulse.agents.extractor.agent import ExtractorAgent
from pulse.config import load_config
from pulse.entities.extracts import ExtractorOutput
from tests.evaluation import (
    Answer,
    Recorder,
    Results,
    corpus_inputs,
    evaluate,
    extractor_expectations,
    extractor_problems,
    save_results,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SAMPLES = 5


async def test_the_extractor_gives_the_expected_answer_for_every_corpus_email() -> None:
    config = load_config(Path("config.yaml"))
    model = config.llm.models["extractor"]
    recorder = Recorder()
    provider = OpenAIProvider(
        base_url=config.llm.base_url,
        api_key=os.environ[config.llm.api_key_env],
        http_client=recorder.client(),
    )
    # As main builds it, with the recording client on the provider.
    agent = ExtractorAgent(OpenAIChatModel(model, provider=provider), config.categories)
    expected = extractor_expectations()
    inputs = corpus_inputs()

    def problems(corpus_id: str, output: ExtractorOutput) -> list[str]:
        return extractor_problems(output, expected[corpus_id])

    started = datetime.now(UTC)
    answers = await evaluate(agent.answer, problems, inputs, SAMPLES, recorder)
    results = Results.of(
        agent="extractor",
        model=model,
        base_url=config.llm.base_url,
        samples=SAMPLES,
        started=started,
        finished=datetime.now(UTC),
        answers=answers,
        recorder=recorder,
    )
    path = save_results(results)

    subjects = {item.corpus_id: item.email.subject for item in inputs}
    for corpus_id, subject in subjects.items():
        _report(corpus_id, subject, [a for a in answers if a.corpus_id == corpus_id])
    calls = sum(len(answer.exchanges) for answer in answers)
    right = sum(1 for answer in answers if not answer.problems)
    print(f"\n{right}/{len(answers)} answers right, {calls} calls; results in {path}")
    assert right == len(answers)


def _report(corpus_id: str, subject: str, answers: list[Answer]) -> None:
    right = sum(1 for answer in answers if not answer.problems)
    retried = sum(1 for answer in answers if len(answer.exchanges) > 1)
    print(f"\n{corpus_id} {subject!r}: {right}/{len(answers)} right, {retried} retried")
    for problem, count in Counter(p for a in answers for p in a.problems).items():
        print(f"    {count}x {problem}")
    outputs = [answer.output for answer in answers if answer.output]
    for field in ("summary", "exclusion_reason"):
        for value, count in Counter(str(output[field]) for output in outputs).items():
            print(f"    {count}x {field}: {value}")
