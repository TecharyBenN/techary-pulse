"""Runs the consolidator on the corpus through the dev gateway, using ./config.yaml and .env."""

import os
from pathlib import Path

import pytest

from pulse.adapters.gateway import gateway_model
from pulse.agents.consolidator.agent import build_consolidator, consolidator_prompt, output_checks
from pulse.agents.runner import run_specialist
from pulse.config import load_config
from pulse.entities.extracts import ExtractorOutput, make_record
from tests.emails import corpus_messages

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_duplicate_reports_merge_into_single_items() -> None:
    config = load_config(Path("config.yaml"))
    model = gateway_model(
        config.llm.base_url, os.environ[config.llm.api_key_env], config.llm.models["consolidator"]
    )
    extracted = [
        make_record(m["id"], ExtractorOutput.model_validate(m["extract"]), None, [])
        for m in corpus_messages()
        if "extract" in m
    ]
    records = [record for record in extracted if record.exclusion is None]

    output = await run_specialist(
        build_consolidator(model),
        consolidator_prompt(records),
        lambda output: output_checks(output, records),
    )

    print(output.model_dump_json(indent=2))
    sources = sorted(sorted(item.source_message_ids) for item in output.items)
    assert sources == [["m01", "m02"], ["m03", "m06"], ["m04"], ["m05"]]
