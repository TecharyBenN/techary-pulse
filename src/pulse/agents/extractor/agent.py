"""The extractor agent, which finds the facts, people and category in one screened email."""

from collections import Counter
from collections.abc import Mapping

from pydantic_ai.models import Model

from pulse.agents.base import EmailAgent
from pulse.entities.extracts import ExtractorOutput, number_problems
from pulse.entities.mail import ScreenedEmail, message_lines


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

    def message(self, task: ScreenedEmail) -> str:
        # The numbered lines replace the body, so the email's text is sent once.
        lines = [{"line": n, "text": text} for n, text in enumerate(message_lines(task))]
        email = self._email_block(task, without=frozenset({"body"}))
        return f"{email}\n\n{self._data_block('lines', lines)}"

    def checks(self, output: ExtractorOutput, task: ScreenedEmail) -> list[str]:
        if coverage := _uncovered(output, len(message_lines(task))):
            return [f"list every line in <lines> once in lines: {coverage}"]
        if output.category is not None and output.exclusion_reason is not None:
            return ["give either a category or an exclusion reason, not both"]
        if output.category is None and output.exclusion_reason is None:
            return ["give either a category or an exclusion reason"]
        if output.category is not None and output.category not in self._categories:
            return [f"category {output.category} is not a configured category"]
        facts = [fact for line in output.lines for fact in line.facts]
        if added := number_problems([], facts, "\n".join(message_lines(task))):
            return [
                f"write each number only as the email gives it, adding none: {', '.join(added)}"
            ]
        return []


def _uncovered(output: ExtractorOutput, count: int) -> str:
    """The line numbers the answer misses, repeats or makes up, or "" when it lists each once."""
    listed = Counter(line.line for line in output.lines)
    parts = [
        (label, sorted(numbers))
        for label, numbers in (
            ("missing", set(range(count)) - set(listed)),
            ("repeated", {n for n, times in listed.items() if times > 1 and n < count}),
            ("not in <lines>", {n for n in listed if not 0 <= n < count}),
        )
        if numbers
    ]
    return "; ".join(f"{label} {', '.join(map(str, numbers))}" for label, numbers in parts)
