"""The base class every agent derives from."""

import inspect
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel


class Agent[InputT, OutputT: BaseModel](ABC):
    """One model step: its instructions, input, output type, output checks and tools.

    Agents never have tools; ``tools`` exists so the rule is stated in one place.
    """

    name: ClassVar[str]
    output_type: type[OutputT]
    tools: ClassVar[tuple[()]] = ()

    def instructions(self) -> str:
        """Return the instructions held in ``instructions.md`` beside the agent's class."""
        path = Path(inspect.getfile(type(self))).with_name("instructions.md")
        return path.read_text(encoding="utf-8")

    @abstractmethod
    def build_message(self, data: InputT) -> str:
        """Turn the agent's input into the message sent to the model."""

    def check_output(self, data: InputT, output: OutputT) -> list[str]:
        """Return reasons the output is invalid beyond its schema; empty if valid."""
        return []

    def subject(self, data: InputT) -> str:
        """Describe the input in error messages, such as the message an extract is for."""
        return self.name
