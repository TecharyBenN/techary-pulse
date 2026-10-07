"""The writer agent, which writes or revises the newsletter's content."""

from collections.abc import Mapping

from pydantic_ai.models import Model

from pulse.agents.base import SpecialistAgent
from pulse.entities.base import Entity
from pulse.entities.content import WriterOutput
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.extracts import ExtractRecord, SourcedItem

# Message IDs are for tools only, so the writer is not given them.
_ITEM_FIELDS = {"item_id", "category", "facts", "people", "sender_names", "received"}
_EXCLUDED_FIELDS = {
    "excluded_id",
    "exclusion",
    "exclusion_reason",
    "category",
    "summary",
    "facts",
    "people",
    "sensitivity",
}


class WriterTask(Entity):
    """What the writer works from. The orchestrator's instruction is model output shaped by
    staff emails and reviewer messages, so it is untrusted too."""

    headline: str | None
    items: list[SourcedItem]
    excluded: list[ExtractRecord]
    feedback: list[ReviewerMessage]
    instruction: str
    # The working draft, which the writer revises when there is one.
    draft: WriterOutput | None


class WriterAgent(SpecialistAgent[WriterTask, WriterOutput]):
    def __init__(
        self,
        model: Model,
        categories: Mapping[str, str],
        section_titles: Mapping[str, str],
        headline_title: str,
        max_words: int,
    ) -> None:
        """`categories` maps each configured category to its definition, and `section_titles`
        each to its default title, in configuration order."""
        super().__init__(
            model,
            name="writer",
            deps_type=WriterTask,
            output_type=WriterOutput,
            # These come from configuration, so they may sit in the instructions.
            instructions=[
                self._categories_instruction(categories),
                f"Unless asked otherwise, use these sections, in this order, with these titles:\n"
                f"{self._listed(section_titles)}",
                f"Unless asked otherwise, the headline title is: {headline_title}",
                f"The newsletter must be under {max_words} words.",
            ],
        )

    def message(self, task: WriterTask) -> str:
        blocks = [
            self._data_block(
                "items",
                {
                    "headline": task.headline,
                    "items": [
                        item.model_dump(mode="json", include=_ITEM_FIELDS) for item in task.items
                    ],
                },
            ),
            self._data_block(
                "excluded_records",
                [r.model_dump(mode="json", include=_EXCLUDED_FIELDS) for r in task.excluded],
            ),
            self._feedback_block(task.feedback),
            self._data_block("instruction", task.instruction),
        ]
        if task.draft is not None:
            blocks.append(
                self._data_block("working_draft", task.draft.content.model_dump(mode="json"))
            )
        return "\n\n".join(blocks)

    def checks(self, output: WriterOutput, task: WriterTask) -> list[str]:
        known = {item.item_id for item in task.items}
        content = output.content
        unknown = [
            f"{item_id} in item_ids is not a known item"
            for item_id in dict.fromkeys(content.item_ids)
            if item_id not in known
        ]
        missing = [
            f"the entry for {entry.item_id} is not in item_ids"
            for entry in content.entries()
            if entry.item_id not in content.item_ids
        ]
        # The fix is always to remove the name: naming senders in the text is not wanted.
        unnamed = [
            f"remove {name} from the people of the {entry.item_id} entry: "
            "its text does not name them"
            for entry in content.entries()
            for name in entry.people
            if name.casefold() not in entry.text.casefold()
        ]
        return unknown + missing + unnamed
