"""The sensitivity agent, which flags the sensitive content in one screened email: its
definition."""

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from pulse.agents.prompts import read_instructions
from pulse.entities.extracts import SensitivityOutput

INSTRUCTIONS = read_instructions(__package__)


def build_sensitivity(model: Model) -> Agent[None, SensitivityOutput]:
    """It reads the same email block as the extractor, from `email_prompt`."""
    return Agent(
        model,
        output_type=NativeOutput(SensitivityOutput),
        instructions=INSTRUCTIONS,
        name="sensitivity",
    )
