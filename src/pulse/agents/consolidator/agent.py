"""The consolidator agent, which merges extract records that report the same news and writes the
headline."""

from collections import Counter
from collections.abc import Mapping

from pydantic_ai.models import Model

from pulse.agents.base import SpecialistAgent
from pulse.entities.base import Entity
from pulse.entities.extracts import ConsolidatorOutput, ExtractRecord

# Every input record is included, so its exclusion fields carry nothing.
_RECORD_FIELDS = {"message_id", "category", "summary", "facts", "people"}


class ConsolidatorTask(Entity):
    """The included extract records."""

    records: list[ExtractRecord]


class ConsolidatorAgent(SpecialistAgent[ConsolidatorTask, ConsolidatorOutput]):
    def __init__(self, model: Model, categories: Mapping[str, str]) -> None:
        """`categories` maps each configured category to its definition, so the consolidator
        can place a restored record that has none."""
        super().__init__(
            model,
            name="consolidator",
            deps_type=ConsolidatorTask,
            output_type=ConsolidatorOutput,
            instructions=[self._categories_instruction(categories)],
        )
        self._categories = categories

    def message(self, task: ConsolidatorTask) -> str:
        data = [record.model_dump(mode="json", include=_RECORD_FIELDS) for record in task.records]
        return self._data_block("extract_records", data)

    def checks(self, output: ConsolidatorOutput, task: ConsolidatorTask) -> list[str]:
        inputs = [record.message_id for record in task.records]
        counts = Counter(
            message_id for item in output.items for message_id in item.source_message_ids
        )
        unknown = [
            f"source {message_id} is not an input record"
            for message_id in counts
            if message_id not in inputs
        ]
        misplaced = [
            f"record {message_id} appears {counts[message_id]} times in the items"
            for message_id in inputs
            if counts[message_id] != 1
        ]
        unconfigured = [
            f"category {category} is not a configured category"
            for category in dict.fromkeys(item.category for item in output.items)
            if category not in self._categories
        ]
        return unknown + misplaced + unconfigured
