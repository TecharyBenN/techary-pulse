"""Evaluates the writer on its own, writing a first draft from the consolidator baseline's items,
through the dev gateway, using ./config.yaml and .env. Every call is paid; the results, with every
request sent, go to evaluations/writer/."""

from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.models import Model

from pulse.agents.writer.agent import WriterAgent
from pulse.config import Config, load_config
from pulse.entities.content import WriterOutput
from tests.evaluation import (
    consolidator_expectations,
    corpus_inputs,
    run_live,
    writer_inputs,
    writer_problems,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

# One call per sample, as the writer writes the whole newsletter at once.
SAMPLES = 10


async def test_the_writer_drafts_every_item_as_expected() -> None:
    config = load_config(Path("config.yaml"))
    # The writer keeps each item's facts, so it is held to the consolidator's expected items.
    expected = consolidator_expectations()["included"]
    inputs = writer_inputs()
    [item] = inputs
    emails = [email.task for email in corpus_inputs()]

    def build(model: Model, config: Config) -> WriterAgent:
        return WriterAgent(
            model,
            config.categories,
            config.section_titles,
            config.headline_title,
            config.limits.max_words,
        )

    def problems(_: str, output: WriterOutput) -> list[str]:
        return writer_problems(
            output, item.task, expected, emails, config.categories, config.limits.max_words
        )

    answers = await run_live("writer", build, inputs, problems, _details, SAMPLES)

    assert all(not answer.problems for answer in answers)


def _details(output: dict[str, Any]) -> list[str]:
    content = output["content"]
    return [f"headline: {content['headline']}", f"intro: {content['intro']}"]
