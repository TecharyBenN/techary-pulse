"""The extractor agent, which finds the facts, people and category in one screened email."""

from collections.abc import Mapping

from pydantic_ai.models import Model

from pulse.agents.base import EmailAgent
from pulse.entities.extracts import ExtractorOutput
from pulse.entities.mail import ScreenedEmail


class ExtractorAgent(EmailAgent[ExtractorOutput]):
    def __init__(self, model: Model, categories: Mapping[str, str]) -> None:
        """`categories` maps each configured category to its definition."""
        super().__init__(
            model,
            name="extractor",
            output_type=ExtractorOutput,
            instructions=[self._categories_instruction(categories)],
        )
        self._categories = categories

    def checks(self, output: ExtractorOutput, task: ScreenedEmail) -> list[str]:
        if output.category is not None and output.exclusion_reason is not None:
            return ["give either a category or an exclusion reason, not both"]
        if output.category is None and output.exclusion_reason is None:
            return ["give either a category or an exclusion reason"]
        if output.category is not None and output.category not in self._categories:
            return [f"category {output.category} is not a configured category"]
        return []
