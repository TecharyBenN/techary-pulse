"""The orchestrator agent, its instructions and its user prompt."""

from importlib.resources import files

from pydantic_ai import Agent
from pydantic_ai.models import Model

from pulse.entities.conversation import ReviewerMessage

# The orchestrator's whole reply when a message needs no answer.
NO_REPLY = "NO_REPLY"

INSTRUCTIONS = files(__package__).joinpath("prompt.md").read_text(encoding="utf-8")


def build_agent(model: Model) -> Agent[None, str]:
    return Agent(model, output_type=str, instructions=INSTRUCTIONS, name="orchestrator")


def user_prompt(message: ReviewerMessage) -> str:
    """The reviewer's text stays inside a delimited block, never in the instructions."""
    return (
        f"Message from reviewer {message.author} through {message.channel}:\n"
        f"<reviewer_message>\n{message.text}\n</reviewer_message>"
    )
