"""The writer agent, which writes or revises the newsletter's content: its definition, prompt and
output checks."""

from collections.abc import Collection, Mapping, Sequence

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.agents.prompts import (
    categories_instruction,
    data_block,
    feedback_block,
    listed,
    read_instructions,
)
from pulse.entities.content import WriterOutput
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.extracts import Consolidation, ExtractRecord, SourcedItem

INSTRUCTIONS = read_instructions(__package__)

# Message IDs are for tools only, so the writer is not given them.
_ITEM_FIELDS = {"item_id", "category", "facts", "people", "sender_names", "received"}
_EXCLUDED_FIELDS = {
    "excluded_id",
    "exclusion",
    "exclusion_reason",
    "category",
    "summary",
    "facts",
    "people",
    "sensitivity",
}


def build_writer(
    model: Model,
    categories: Mapping[str, str],
    section_titles: Mapping[str, str],
    headline_title: str,
    max_words: int,
) -> Agent[None, WriterOutput]:
    """`categories` maps each configured category to its definition, and `section_titles`
    each to its default title, in configuration order."""
    return Agent(
        model,
        output_type=NativeOutput(WriterOutput),
        # These come from configuration, so they may sit in the instructions.
        instructions=[
            INSTRUCTIONS,
            categories_instruction(categories),
            f"Unless asked otherwise, use these sections, in this order, with these titles:\n"
            f"{listed(section_titles)}",
            f"Unless asked otherwise, the headline title is: {headline_title}",
            f"The newsletter must be under {max_words} words.",
        ],
        name="writer",
    )


def writer_prompt(
    consolidation: Consolidation | None,
    items: Sequence[SourcedItem],
    excluded: Sequence[ExtractRecord],
    feedback: Sequence[ReviewerMessage],
    instruction: str,
    draft: WriterOutput | None,
) -> str:
    """Every input stays inside a delimited block, never in the instructions.

    The orchestrator's instruction is model output shaped by staff emails and reviewer
    messages, so it is untrusted too.
    """
    blocks = [
        data_block(
            "items",
            {
                "headline": consolidation.headline if consolidation else None,
                "items": [item.model_dump(mode="json", include=_ITEM_FIELDS) for item in items],
            },
        ),
        data_block(
            "excluded_records",
            [record.model_dump(mode="json", include=_EXCLUDED_FIELDS) for record in excluded],
        ),
        feedback_block(feedback),
        data_block("instruction", instruction),
    ]
    if draft is not None:
        blocks.append(data_block("working_draft", draft.content.model_dump(mode="json")))
    return "\n\n".join(blocks)


def output_checks(output: WriterOutput, known_ids: Collection[str]) -> list[str]:
    """`known_ids` holds the item IDs of the newsletter."""
    content = output.content
    unknown = [
        f"{item_id} in item_ids is not a known item"
        for item_id in dict.fromkeys(content.item_ids)
        if item_id not in known_ids
    ]
    missing = [
        f"the entry for {entry.item_id} is not in item_ids"
        for entry in content.entries()
        if entry.item_id not in content.item_ids
    ]
    return unknown + missing
