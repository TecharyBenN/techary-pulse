"""The four agents, one per model step."""

from pulse.agents.consolidator.agent import Consolidator
from pulse.agents.drafter.agent import Drafter
from pulse.agents.extractor.agent import Extractor
from pulse.agents.judge.agent import Judge

AGENT_NAMES = frozenset(agent.name for agent in (Extractor, Consolidator, Drafter, Judge))

__all__ = ["AGENT_NAMES", "Consolidator", "Drafter", "Extractor", "Judge"]
