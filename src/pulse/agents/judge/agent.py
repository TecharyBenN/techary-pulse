"""The judge: says whether the intro and each entry are supported by the facts and feedback."""

from collections import Counter
from collections.abc import Sequence

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.agents.prompts import data_block, feedback_block, read_instructions
from pulse.entities.content import Content, JudgeOutput
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
    content: Content, items: Sequence[SourcedItem], feedback: Sequence[ReviewerMessage]
) -> str:
    """Every input stays inside a delimited block, never in the instructions."""
    draft = {
        "intro": content.intro,
        "entries": [{"item_id": e.item_id, "text": e.text} for e in content.entries()],
    }
    return "\n\n".join(
        [
            data_block("draft", draft),
            data_block(
                "items", [item.model_dump(mode="json", include=_ITEM_FIELDS) for item in items]
            ),
            feedback_block(feedback),
        ]
    )


def output_checks(output: JudgeOutput, content: Content) -> list[str]:
    """The intro and every entry each need exactly one verdict."""
    targets = ["intro", *(entry.item_id for entry in content.entries())]
    counts = Counter(verdict.target for verdict in output.verdicts)
    unknown = [
        f"{target} is not the intro or an entry's item ID"
        for target in counts
        if target not in targets
    ]
    miscounted = [
        f"{target} has {counts[target]} verdicts" for target in targets if counts[target] != 1
    ]
    claims = [
        f"the verdict on {verdict.target} must name a claim exactly when it is not supported"
        for verdict in output.verdicts
        if verdict.supported == (verdict.claim is not None)
    ]
    return unknown + miscounted + claims
