"""The extractor: finds the facts, people, category and sensitivity in one screened email."""

import json
from collections.abc import Mapping
from importlib.resources import files

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.entities.extracts import ExtractorOutput
from pulse.entities.mail import ScreenedEmail

INSTRUCTIONS = files(__package__).joinpath("prompt.md").read_text(encoding="utf-8")

# Code attaches the message ID to the record, so the model is not given it.
_PROMPT_FIELDS = {"sender_name", "sender_address", "subject", "received", "body"}


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


def extractor_prompt(email: ScreenedEmail) -> str:
    """The email stays inside a delimited block, never in the instructions."""
    data = email.model_dump(mode="json", include=_PROMPT_FIELDS)
    return f"<email>\n{json.dumps(data, ensure_ascii=False)}\n</email>"


def output_checks(output: ExtractorOutput, categories: Mapping[str, str]) -> list[str]:
    if output.category is not None and output.exclusion_reason is not None:
        return ["give either a category or an exclusion reason, not both"]
    if output.category is None and output.exclusion_reason is None:
        return ["give either a category or an exclusion reason"]
    if output.category is not None and output.category not in categories:
        return [f"category {output.category} is not a configured category"]
    return []
