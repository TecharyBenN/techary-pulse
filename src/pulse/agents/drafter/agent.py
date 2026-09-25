from pulse.agents.base import Agent
from pulse.models import Draft, DraftInput


class Drafter(Agent[DraftInput, Draft]):
    """Writes the newsletter from the consolidated items."""

    name = "drafter"
    output_type = Draft

    def build_message(self, data: DraftInput) -> str:
        return data.model_dump_json(indent=2)
