"""Runs the consolidator on the corpus through the dev gateway, using ./config.yaml and .env."""

import os
from pathlib import Path

import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pulse.agents.consolidator.agent import ConsolidatorAgent, ConsolidatorTask
from pulse.config import load_config
from pulse.entities.extracts import Extraction, make_record
from tests.emails import CORPUS_ITEM_SOURCES, corpus_messages

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_duplicate_reports_merge_into_single_items() -> None:
    config = load_config(Path("config.yaml"))
    provider = OpenAIProvider(
        base_url=config.llm.base_url, api_key=os.environ[config.llm.api_key_env]
    )
    model = OpenAIChatModel(config.llm.models["consolidator"], provider=provider)
    extracted = [
        make_record(m["id"], Extraction.model_validate(m["extract"]), [])
        for m in corpus_messages()
        if "extract" in m
    ]
    records = [record for record in extracted if record.exclusion is None]

    output = await ConsolidatorAgent(model, config.categories).answer(
        ConsolidatorTask(records=records)
    )

    print(output.model_dump_json(indent=2))
    sources = sorted(sorted(item.source_message_ids) for item in output.items)
    assert sources == sorted(sorted(item) for item in CORPUS_ITEM_SOURCES)
