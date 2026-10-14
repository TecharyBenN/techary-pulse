"""Evaluates the writer on its own, writing a first draft from the consolidator baseline's items,
and a first draft and a fix for a dense supplier notice, through the dev gateway, using
./config.yaml and .env. Every call is paid; the results, with every request sent, go to
evaluations/writer/."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.models import Model

from pulse.agents.writer.agent import WriterAgent
from pulse.config import Config, load_config
from pulse.entities.content import WriterOutput
from tests.evaluation import (
    STORE_VARIABLE,
    ConsolidatorExpectation,
    corpus_inputs,
    notice_email,
    run_live,
    saved_run,
    saved_writer_inputs,
    writer_expectations,
    writer_inputs,
    writer_problems,
)

pytestmark = [pytest.mark.live, pytest.mark.anyio]

# One call per sample, as the writer writes the whole newsletter at once.
SAMPLES = 10


@pytest.mark.skipif(STORE_VARIABLE in os.environ, reason="a saved run's store is given")
async def test_the_writer_drafts_every_item_as_expected() -> None:
    config = load_config(Path("config.yaml"))
    # The writer keeps each item's facts, so it is held to the items expected for its input.
    expected = writer_expectations()
    inputs = writer_inputs()
    tasks = {item.input_id: item.task for item in inputs}
    emails = [*(email.task for email in corpus_inputs()), notice_email()]

    def problems(input_id: str, output: WriterOutput) -> list[str]:
        return writer_problems(
            output,
            tasks[input_id],
            expected[input_id],
            emails,
            config.categories,
            config.limits.max_words,
        )

    answers = await run_live("writer", _build, inputs, problems, _details, SAMPLES)

    assert all(not answer.problems for answer in answers)


@pytest.mark.skipif(STORE_VARIABLE not in os.environ, reason="needs a saved run's store")
async def test_the_writer_drafts_a_saved_run_first_draft() -> None:
    """The saved run has no expected answer: Pulse's own draft checks are scored, and the drafts
    are read against the items' facts."""
    config = load_config(Path("config.yaml"))
    run = await saved_run(Path(os.environ[STORE_VARIABLE]))

    def problems(_: str, output: WriterOutput) -> list[str]:
        no_items = ConsolidatorExpectation(items=[])
        return writer_problems(
            output, run.task, no_items, run.emails, config.categories, config.limits.max_words
        )

    await run_live("writer", _build, saved_writer_inputs(run), problems, _details, SAMPLES)


def _build(models: Callable[[str], Model], config: Config) -> WriterAgent:
    return WriterAgent(
        models("writer"),
        config.categories,
        config.section_titles,
        config.headline_title,
        config.limits.max_words,
    )


def _details(output: dict[str, Any]) -> list[str]:
    content = output["content"]
    entries = [entry["text"] for section in content["sections"] for entry in section["entries"]]
    lines = [f"headline: {content['headline']}", f"intro: {content['intro']}"]
    # A single-item draft's entry is what its input tests.
    return lines + ([f"entry: {entries[0]}"] if len(entries) == 1 else [])
