"""The consolidator agent, which merges extract records that report the same news and writes the
headline."""

from collections import Counter
from collections.abc import Mapping

from pydantic_ai.models import Model

from pulse.agents.base import SpecialistAgent
from pulse.entities.base import Entity
from pulse.entities.extracts import ConsolidatorOutput, ExtractRecord, numbered_records

# Every input record is included, so its exclusion fields carry nothing, and its record ID
# stands in for its message ID.
_RECORD_FIELDS = {"category", "summary", "facts", "people"}


class ConsolidatorTask(Entity):
    """The included extract records."""

    records: list[ExtractRecord]


class ConsolidatorAgent(SpecialistAgent[ConsolidatorTask, ConsolidatorOutput]):
    def __init__(self, model: Model, categories: Mapping[str, str]) -> None:
        """`categories` maps each configured category to its definition."""
        super().__init__(
            model,
            name="consolidator",
            deps_type=ConsolidatorTask,
            output_type=ConsolidatorOutput,
            instructions=[self._categories_instruction(categories)],
        )
        self._categories = categories

    def message(self, task: ConsolidatorTask) -> str:
        data = [
            {"record_id": record_id, **record.model_dump(mode="json", include=_RECORD_FIELDS)}
            for record_id, record in numbered_records(task.records).items()
        ]
        return self._data_block("extract_records", data)

    def checks(self, output: ConsolidatorOutput, task: ConsolidatorTask) -> list[str]:
        inputs = list(numbered_records(task.records))
        counts = Counter(record_id for item in output.items for record_id in item.source_record_ids)
        unknown = [
            f"source {record_id} is not an input record"
            for record_id in counts
            if record_id not in inputs
        ]
        misplaced = [
            f"record {record_id} appears {counts[record_id]} times in the items"
            for record_id in inputs
            if counts[record_id] != 1
        ]
        unconfigured = [
            f"category {category} is not a configured category"
            for category in dict.fromkeys(item.category for item in output.items)
            if category not in self._categories
        ]
        return unknown + misplaced + unconfigured
