"""The orchestrator agent, its instructions and its user prompt."""

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


def user_prompt(message: ReviewerMessage) -> str:
    """The reviewer's text stays inside a delimited block, never in the instructions."""
    return (
        f"Message from reviewer {message.author} through {message.channel}:\n"
        f"<reviewer_message>\n{message.text}\n</reviewer_message>"
    )
