"""The base class every agent derives from."""

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel


class Agent[InputT, OutputT: BaseModel](ABC):
    """One model step: its instructions, input, output type, output checks and tools.

    Agents never have tools; ``tools`` exists so the rule is stated in one place.
    """

    name: ClassVar[str]
    output_type: type[OutputT]
    tools: ClassVar[tuple[()]] = ()
    INSTRUCTIONS: ClassVar[str]

    def instructions(self) -> str:
        """Return the instructions sent to the model."""
        return self.INSTRUCTIONS

    @abstractmethod
    def build_message(self, data: InputT) -> str:
        """Turn the agent's input into the message sent to the model."""

    def check_output(self, data: InputT, output: OutputT) -> list[str]:
        """Return reasons the output is invalid beyond its schema; empty if valid."""
        return []

    def subject(self, data: InputT) -> str:
        """Describe the input in error messages, such as the message an extract is for."""
        return self.name
