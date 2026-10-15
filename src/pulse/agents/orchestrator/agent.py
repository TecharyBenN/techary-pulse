"""The orchestrator agent, which runs the newsletter conversation through its tools."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset

from pulse.agents.base import PulseAgent
from pulse.entities.conversation import RunMessage, StartInstruction

# The orchestrator's whole reply when a message needs no answer.
NO_REPLY = "NO_REPLY"

START_INSTRUCTION = "Draft a newsletter."
_START_SOURCES = {"scheduler": "the scheduler", "pulse draft": "pulse draft"}


class OrchestratorAgent(PulseAgent[RunMessage, str]):
    """Each run's dependency is the message it answers, so tools take the caller from code."""

    def __init__(self, model: Model, toolset: AbstractToolset[RunMessage]) -> None:
        super().__init__(
            model,
            name="orchestrator",
            deps_type=RunMessage,
            output_type=str,
            toolsets=[toolset],
        )

    @staticmethod
    def message(
        run_message: RunMessage, state: Mapping[str, Any] | None, recap: bool = False
    ) -> str:
        """The reviewer's text stays inside a delimited block, never in the instructions. The
        lines before it are written by code, so the orchestrator can rely on them: `state` is
        the latest newsletter's summary, or None when there is none. The start instruction is
        written by code alone, so it needs no block."""
        state_line = (
            "No newsletter exists yet."
            if state is None
            else f"The newsletter now: {json.dumps(state, ensure_ascii=False)}"
        )
        if isinstance(run_message, StartInstruction):
            source = _START_SOURCES[run_message.source]
            return "\n".join([f"Message from {source}:", state_line, START_INSTRUCTION])
        lines = [
            f"Message from reviewer {run_message.author} through {run_message.channel}:",
            state_line,
        ]
        if recap:
            lines.append("Start your reply with a short recap of where the newsletter stands.")
        return "\n".join([*lines, "<reviewer_message>", run_message.text, "</reviewer_message>"])
