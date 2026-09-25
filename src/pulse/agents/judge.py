from pulse.agents.base import Agent
from pulse.models import JudgeInput, JudgeResult


class Judge(Agent[JudgeInput, JudgeResult]):
    """Verifies the intro and each entry against the consolidated items."""

    name = "judge"
    output_type = JudgeResult
    INSTRUCTIONS = """\
You check a draft of Techary Pulse, the internal staff newsletter, against the consolidated
items it was written from. You receive the draft and the items as JSON.

- For the intro, set `supported` to `true` only if everything it says is supported by the facts
  of the items.
- For each entry, return its `item_id` and set `supported` to `true` only if everything its text
  says is supported by the facts of that item.
- When `supported` is `false`, give the claim that is not supported in `reason`. When it is
  `true`, leave `reason` empty.

Each item's `sender_names` are the people who sent the update in, and every entry must name them.
Crediting a sender with sharing or reporting their update is supported.

Judge only whether the text is supported by the facts. Do not judge style or tone.
"""

    def build_message(self, data: JudgeInput) -> str:
        return data.model_dump_json(indent=2)
