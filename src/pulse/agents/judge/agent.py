from pulse.agents.base import Agent
from pulse.models import JudgeInput, JudgeResult


class Judge(Agent[JudgeInput, JudgeResult]):
    """Verifies the intro and each entry against the consolidated items."""

    name = "judge"
    output_type = JudgeResult

    def build_message(self, data: JudgeInput) -> str:
        return data.model_dump_json(indent=2)
