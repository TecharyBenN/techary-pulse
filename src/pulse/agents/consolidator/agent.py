"""The consolidator: merges extract records that report the same news and writes the headline."""

import json
from collections import Counter
from collections.abc import Sequence
from importlib.resources import files

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.entities.extracts import ConsolidatorOutput, ExtractRecord

INSTRUCTIONS = files(__package__).joinpath("prompt.md").read_text(encoding="utf-8")

# Every input record is included, so its exclusion fields carry nothing.
_PROMPT_FIELDS = {"message_id", "category", "summary", "facts", "people"}


def build_consolidator(model: Model) -> Agent[None, ConsolidatorOutput]:
    return Agent(
        model,
        output_type=NativeOutput(ConsolidatorOutput),
        instructions=INSTRUCTIONS,
        name="consolidator",
    )


def consolidator_prompt(records: Sequence[ExtractRecord]) -> str:
    """The records stay inside a delimited block, never in the instructions."""
    data = [record.model_dump(mode="json", include=_PROMPT_FIELDS) for record in records]
    return f"<extract_records>\n{json.dumps(data, ensure_ascii=False)}\n</extract_records>"


def output_checks(output: ConsolidatorOutput, records: Sequence[ExtractRecord]) -> list[str]:
    inputs = [record.message_id for record in records]
    counts = Counter(message_id for item in output.items for message_id in item.source_message_ids)
    unknown = [
        f"source {message_id} is not an input record"
        for message_id in counts
        if message_id not in inputs
    ]
    misplaced = [
        f"record {message_id} appears {counts[message_id]} times in the items"
        for message_id in inputs
        if counts[message_id] != 1
    ]
    return unknown + misplaced
