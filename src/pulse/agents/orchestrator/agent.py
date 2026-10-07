"""The orchestrator agent, which runs the newsletter conversation through its tools."""

from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset

from pulse.agents.base import PulseAgent
from pulse.entities.conversation import ReviewerMessage

# The orchestrator's whole reply when a message needs no answer.
NO_REPLY = "NO_REPLY"


class OrchestratorAgent(PulseAgent[ReviewerMessage, str]):
    """Each run's dependency is the message it answers, so tools take the caller from code."""

    def __init__(self, model: Model, toolset: AbstractToolset[ReviewerMessage]) -> None:
        super().__init__(
            model,
            name="orchestrator",
            deps_type=ReviewerMessage,
            output_type=str,
            toolsets=[toolset],
        )

    @staticmethod
    def message(reviewer_message: ReviewerMessage, recap: bool = False) -> str:
        """The reviewer's text stays inside a delimited block, never in the instructions. The
        line before it is written by code, so the orchestrator can rely on it."""
        lines = [
            f"Message from reviewer {reviewer_message.author} through {reviewer_message.channel}:"
        ]
        if recap:
            lines.append("Start your reply with a short recap of where the newsletter stands.")
        return "\n".join(
            [*lines, "<reviewer_message>", reviewer_message.text, "</reviewer_message>"]
        )
