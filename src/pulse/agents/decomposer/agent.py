"""The decomposer agent, which splits one line's facts so each states one thing."""

from pydantic_ai.models import Model

from pulse.agents.base import SpecialistAgent
from pulse.entities.base import Entity
from pulse.entities.extracts import DecomposerOutput, number_problems


class DecomposerTask(Entity):
    """One line's facts, with the line they came from."""

    # The line is for the checks only; the decomposer is never given the email's text.
    line: str
    facts: list[str]


class DecomposerAgent(SpecialistAgent[DecomposerTask, DecomposerOutput]):
    def __init__(self, model: Model) -> None:
        super().__init__(
            model, name="decomposer", deps_type=DecomposerTask, output_type=DecomposerOutput
        )

    def message(self, task: DecomposerTask) -> str:
        return self._data_block("facts", task.facts)

    def checks(self, output: DecomposerOutput, task: DecomposerTask) -> list[str]:
        return number_problems(task.facts, output.facts, task.line)
