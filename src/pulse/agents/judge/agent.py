"""The judge agent, which says whether one text, the intro or an entry, is supported by its facts
and the feedback: its definition, prompt and output checks."""

from collections.abc import Sequence
from typing import Literal

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.agents.prompts import data_block, feedback_block, keyed, read_instructions
from pulse.entities.content import JudgeOutput
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.extracts import SourcedItem

INSTRUCTIONS = read_instructions(__package__)

# Only what supports a claim: the facts, the people they name and who sent them.
_ITEM_FIELDS = {"item_id", "facts", "people", "sender_names"}


def build_judge(model: Model) -> Agent[None, JudgeOutput]:
    return Agent(
        model, output_type=NativeOutput(JudgeOutput), instructions=INSTRUCTIONS, name="judge"
    )


def judge_prompt(
    part: Literal["intro", "entry"],
    text: str,
    items: Sequence[SourcedItem],
    feedback: Sequence[ReviewerMessage],
) -> str:
    """One text with the items whose facts may support it: every item for the intro, its own
    for an entry. Every input stays inside a delimited block, never in the instructions."""
    return "\n\n".join(
        [
            data_block("text", {"part": part, "text": text}),
            data_block("items", [keyed(item, "item_id", _ITEM_FIELDS) for item in items]),
            feedback_block(feedback),
        ]
    )
