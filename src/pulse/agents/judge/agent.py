"""The judge agent, which says whether one text, the intro or an entry, is supported by its facts
and the feedback."""

from typing import Literal

from pydantic_ai.models import Model

from pulse.agents.base import SpecialistAgent
from pulse.entities.base import Entity
from pulse.entities.content import JudgeOutput
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.extracts import SourcedItem

# Only what supports a claim: the facts, the people they name and who sent them.
_ITEM_FIELDS = {"item_id", "facts", "people", "sender_names"}


class JudgeTask(Entity):
    """One text with the items whose facts may support it: every item for the intro, its own
    for an entry."""

    part: Literal["intro", "entry"]
    text: str
    items: list[SourcedItem]
    feedback: list[ReviewerMessage]


class JudgeAgent(SpecialistAgent[JudgeTask, JudgeOutput]):
    """The output type is the whole contract: code decides support from the claims."""

    def __init__(self, model: Model) -> None:
        super().__init__(model, name="judge", deps_type=JudgeTask, output_type=JudgeOutput)

    def message(self, task: JudgeTask) -> str:
        return "\n\n".join(
            [
                self._data_block("text", {"part": task.part, "text": task.text}),
                self._data_block(
                    "items",
                    [item.model_dump(mode="json", include=_ITEM_FIELDS) for item in task.items],
                ),
                self._feedback_block(task.feedback),
            ]
        )
