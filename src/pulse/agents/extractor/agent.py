"""The extractor: finds the facts, people, category and sensitivity in one submission."""

import json
from collections.abc import Mapping
from importlib.resources import files

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.entities.extracts import ExtractorOutput
from pulse.entities.submissions import Submission

INSTRUCTIONS = files(__package__).joinpath("prompt.md").read_text(encoding="utf-8")

_PROMPT_FIELDS = {"message_id", "sender_name", "sender_address", "subject", "received", "body"}


def build_extractor(model: Model, categories: Mapping[str, str]) -> Agent[None, ExtractorOutput]:
    """`categories` maps each configured category to its definition."""
    definitions = "\n".join(f"- {category}: {text}" for category, text in categories.items())
    return Agent(
        model,
        output_type=NativeOutput(ExtractorOutput),
        # The category definitions come from configuration, so they may sit in the instructions.
        instructions=[INSTRUCTIONS, f"The configured categories are:\n{definitions}"],
        name="extractor",
    )


def extractor_prompt(submission: Submission) -> str:
    """The submission stays inside a delimited block, never in the instructions."""
    data = submission.model_dump(mode="json", include=_PROMPT_FIELDS)
    return f"<submission>\n{json.dumps(data, ensure_ascii=False)}\n</submission>"


def output_checks(
    output: ExtractorOutput, submission: Submission, categories: Mapping[str, str]
) -> list[str]:
    problems = []
    if output.message_id != submission.message_id:
        problems.append(f"message_id {output.message_id} is not the submission's message ID")
    if output.category is not None and output.category not in categories:
        problems.append(f"category {output.category} is not a configured category")
    return problems
