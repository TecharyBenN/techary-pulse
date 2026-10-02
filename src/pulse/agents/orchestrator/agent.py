"""The orchestrator agent: its definition and user prompt."""

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset

from pulse.agents.prompts import read_instructions
from pulse.entities.conversation import ReviewerMessage

# The orchestrator's whole reply when a message needs no answer.
NO_REPLY = "NO_REPLY"

INSTRUCTIONS = read_instructions(__package__)


def build_agent(
    model: Model, toolset: AbstractToolset[ReviewerMessage]
) -> Agent[ReviewerMessage, str]:
    """Each run's dependency is the message it answers, so tools take the caller from code."""
    return Agent(
        model,
        deps_type=ReviewerMessage,
        output_type=str,
        instructions=INSTRUCTIONS,
        toolsets=[toolset],
        name="orchestrator",
    )


def user_prompt(message: ReviewerMessage, recap: bool = False) -> str:
    """The reviewer's text stays inside a delimited block, never in the instructions. The line
    before it is written by code, so the orchestrator can rely on it."""
    lines = [f"Message from reviewer {message.author} through {message.channel}:"]
    if recap:
        lines.append("Start your reply with a short recap of where the newsletter stands.")
    return "\n".join([*lines, "<reviewer_message>", message.text, "</reviewer_message>"])
