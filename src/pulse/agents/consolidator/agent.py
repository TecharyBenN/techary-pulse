"""The consolidator agent, which merges extract records that report the same news and writes the
headline: its definition, prompt and output checks."""

from collections import Counter
from collections.abc import Mapping, Sequence

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.agents.prompts import categories_instruction, data_block, read_instructions
from pulse.entities.extracts import ConsolidatorOutput, ExtractRecord

INSTRUCTIONS = read_instructions(__package__)

# Every input record is included, so its exclusion fields carry nothing.
_PROMPT_FIELDS = {"message_id", "category", "summary", "facts", "people"}


def build_consolidator(
    model: Model, categories: Mapping[str, str]
) -> Agent[None, ConsolidatorOutput]:
    """`categories` maps each configured category to its definition, so the consolidator can
    place a restored record that has none."""
    return Agent(
        model,
        output_type=NativeOutput(ConsolidatorOutput),
        instructions=[INSTRUCTIONS, categories_instruction(categories)],
        name="consolidator",
    )


def consolidator_prompt(records: Sequence[ExtractRecord]) -> str:
    """The records stay inside a delimited block, never in the instructions."""
    data = [record.model_dump(mode="json", include=_PROMPT_FIELDS) for record in records]
    return data_block("extract_records", data)


def output_checks(
    output: ConsolidatorOutput, records: Sequence[ExtractRecord], categories: Mapping[str, str]
) -> list[str]:
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
    unconfigured = [
        f"category {category} is not a configured category"
        for category in dict.fromkeys(item.category for item in output.items)
        if category not in categories
    ]
    return unknown + misplaced + unconfigured
