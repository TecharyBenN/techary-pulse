"""The decomposer agent, which splits one line's facts so each states one thing."""

from pydantic_ai.models import Model

from pulse.agents.base import SpecialistAgent, all_finished
from pulse.entities.base import Entity
from pulse.entities.extracts import DecomposerOutput, ExtractorOutput, LineFacts, number_problems
from pulse.entities.mail import ScreenedEmail, message_lines


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

    async def split(self, output: ExtractorOutput, email: ScreenedEmail) -> ExtractorOutput:
        """The extractor's answer with each line's facts split, one call per line with facts."""
        lines = message_lines(email)
        with_facts = [line for line in output.lines if line.facts and 0 <= line.line < len(lines)]
        answers = await all_finished(
            self.answer(DecomposerTask(line=lines[line.line], facts=line.facts))
            for line in with_facts
        )
        split = {line.line: answer.facts for line, answer in zip(with_facts, answers, strict=True)}
        return output.model_copy(
            update={
                "lines": [
                    LineFacts(line=line.line, facts=split.get(line.line, line.facts))
                    for line in output.lines
                ]
            }
        )
