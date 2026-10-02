"""The extractor agent, which finds the facts, people, category and sensitivity in one screened
email: its definition, prompt and output checks."""

from collections.abc import Mapping

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.agents.prompts import categories_instruction, data_block, read_instructions
from pulse.entities.extracts import ExtractorOutput
from pulse.entities.mail import ScreenedEmail

INSTRUCTIONS = read_instructions(__package__)

# Code attaches the message ID to the record, so the model is not given it.
_PROMPT_FIELDS = {"sender_name", "sender_address", "subject", "received", "body"}


def build_extractor(model: Model, categories: Mapping[str, str]) -> Agent[None, ExtractorOutput]:
    """`categories` maps each configured category to its definition."""
    return Agent(
        model,
        output_type=NativeOutput(ExtractorOutput),
        instructions=[INSTRUCTIONS, categories_instruction(categories)],
        name="extractor",
    )


def extractor_prompt(email: ScreenedEmail) -> str:
    """The email stays inside a delimited block, never in the instructions."""
    return data_block("email", email.model_dump(mode="json", include=_PROMPT_FIELDS))


def output_checks(output: ExtractorOutput, categories: Mapping[str, str]) -> list[str]:
    if output.category is not None and output.exclusion_reason is not None:
        return ["give either a category or an exclusion reason, not both"]
    if output.category is None and output.exclusion_reason is None:
        return ["give either a category or an exclusion reason"]
    if output.category is not None and output.category not in categories:
        return [f"category {output.category} is not a configured category"]
    return []
