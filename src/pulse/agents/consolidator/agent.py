import json
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pulse.agents.base import Agent
from pulse.models import Consolidation, ExtractRecord

if TYPE_CHECKING:
    from pulse.config import SectionConfig


class Consolidator(Agent[Sequence[ExtractRecord], Consolidation]):
    """Merges records describing the same news and writes the headline."""

    name = "consolidator"
    output_type = Consolidation

    def __init__(self, sections: Sequence[SectionConfig]) -> None:
        self._sections = sections

    def build_message(self, data: Sequence[ExtractRecord]) -> str:
        return json.dumps([record.model_dump() for record in data], indent=2)

    def check_output(self, data: Sequence[ExtractRecord], output: Consolidation) -> list[str]:
        failures = []
        inputs = {record.message_id for record in data}
        counts = Counter(i for item in output.items for i in item.source_message_ids)
        unknown = sorted(set(counts) - inputs)
        if unknown:
            failures.append(f"unknown source_message_ids: {', '.join(unknown)}")
        missing = sorted(inputs - set(counts))
        if missing:
            failures.append(f"records missing from every item: {', '.join(missing)}")
        repeated = sorted(i for i, n in counts.items() if n > 1)
        if repeated:
            failures.append(f"records in more than one item: {', '.join(repeated)}")
        categories = {s.category for s in self._sections}
        for item in output.items:
            if item.category not in categories:
                failures.append(f"item {item.item_id} category {item.category} is not configured")
        return failures
