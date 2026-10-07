"""The sensitivity agent, which flags the sensitive content in one screened email."""

from pydantic_ai.models import Model

from pulse.agents.base import EmailAgent
from pulse.entities.extracts import SensitivityOutput


class SensitivityAgent(EmailAgent[SensitivityOutput]):
    """It reads the same email message as the extractor."""

    def __init__(self, model: Model) -> None:
        super().__init__(model, name="sensitivity", output_type=SensitivityOutput)
