"""The pipeline agents, one per model step, and the chat agent's name."""

from pulse.agents.consolidator import Consolidator
from pulse.agents.drafter import Drafter
from pulse.agents.extractor import Extractor
from pulse.agents.judge import Judge
from pulse.agents.reviser import Reviser

# The chat agent's key in llm.models. It is not a pipeline Agent, so it is named here.
CHAT = "chat"

AGENT_NAMES = frozenset(
    agent.name for agent in (Extractor, Consolidator, Drafter, Judge, Reviser)
) | {CHAT}

__all__ = ["AGENT_NAMES", "CHAT", "Consolidator", "Drafter", "Extractor", "Judge", "Reviser"]
