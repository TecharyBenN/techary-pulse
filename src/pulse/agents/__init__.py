"""The four agents, one per model step."""

from pulse.agents.consolidator import Consolidator
from pulse.agents.drafter import Drafter
from pulse.agents.extractor import Extractor
from pulse.agents.judge import Judge

AGENT_NAMES = frozenset(agent.name for agent in (Extractor, Consolidator, Drafter, Judge))

__all__ = ["AGENT_NAMES", "Consolidator", "Drafter", "Extractor", "Judge"]
