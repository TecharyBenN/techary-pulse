from pulse.agents.base import Agent
from pulse.models import Draft, DraftInput


class Drafter(Agent[DraftInput, Draft]):
    """Writes the newsletter from the consolidated items."""

    name = "drafter"
    output_type = Draft
    INSTRUCTIONS = """\
You write this week's Techary Pulse, the internal staff newsletter, from the consolidated items
you are given as JSON. Each item has its facts, the people involved and `sender_names`, the
people who sent it in.

Return an `intro` and the `sections`. Each section has a `category` from the items and one entry
per item in that category. Every item appears in exactly one entry, and each entry gives the
`item_id` it is written from.

Rules for every entry:

- One or two sentences.
- Name every sender of its item, from `sender_names`.
- List every person the entry names in `people`, spelt exactly as in the text.
- Use only the facts of its item, with no figures beyond those facts.

Rules for the whole newsletter:

- Keep it under `max_words` words.
- Use everyday language that is genuine and people-focused, with no jargon and no hype words.
  Hype words praise or inflate instead of saying what happened, such as significant, major,
  outstanding, exciting, incredible or huge. State what happened plainly and let the facts speak
  for themselves.
- Keep the tone warm and professional, celebrating people by name.
- Write in British English.
- Never use em dashes or en dashes. Write each sentence so that it does not need one.

If `failures` is not empty, an earlier draft failed these checks. Write a new draft that fixes
every failure listed.
"""

    def build_message(self, data: DraftInput) -> str:
        return data.model_dump_json(indent=2)
