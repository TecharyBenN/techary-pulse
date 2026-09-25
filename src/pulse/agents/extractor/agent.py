from collections.abc import Sequence
from typing import TYPE_CHECKING

from pulse.agents.base import Agent
from pulse.models import CleanedEmail, ExtractRecord

if TYPE_CHECKING:
    from pulse.config import SectionConfig


class Extractor(Agent[CleanedEmail, ExtractRecord]):
    """Turns one cleaned email into an extract record."""

    name = "extractor"
    output_type = ExtractRecord

    def __init__(self, sections: Sequence[SectionConfig]) -> None:
        self._sections = sections

    def instructions(self) -> str:
        categories = "\n".join(f"- {s.category}: {s.definition}" for s in self._sections)
        return f"{super().instructions()}\n\nCategories:\n{categories}\n"

    def build_message(self, data: CleanedEmail) -> str:
        return (
            f'<email message_id="{data.message_id}">\n'
            f"From: {data.sender_name} <{data.sender_address}>\n"
            f"Subject: {data.subject}\n"
            f"Received: {data.received_at.isoformat()}\n\n"
            f"{data.body}\n"
            "</email>"
        )

    def check_output(self, data: CleanedEmail, output: ExtractRecord) -> list[str]:
        failures = []
        if output.message_id != data.message_id:
            failures.append(f"message_id must be {data.message_id}")
        categories = {s.category for s in self._sections}
        if output.category is not None and output.category not in categories:
            failures.append(f"category {output.category} is not configured")
        return failures

    def subject(self, data: CleanedEmail) -> str:
        return f"message {data.message_id}"
