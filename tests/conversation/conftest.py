from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from pulse.agents.runner import AgentRunner
from pulse.config import Config, load_config
from pulse.store import EditionStore

from ..support import DRAFT_V1, JUDGE_PASS, FakeMailbox, at, message, scripted


@pytest.fixture
def config(config_dir: Path, tmp_path: Path) -> Config:
    base = load_config(config_dir / "config.example.yaml")
    return base.model_copy(update={"run_artefacts_dir": tmp_path / "runs"})


@pytest.fixture
def submissions() -> FakeMailbox:
    return FakeMailbox([])


@pytest.fixture
def conversation() -> FakeMailbox:
    return FakeMailbox([])


@pytest.fixture
def store(tmp_path: Path) -> EditionStore:
    return EditionStore(tmp_path / "state" / "pulse.db")


def clock(day: int = 25, hour: int = 9, minute: int = 0) -> Callable[[], datetime]:
    return lambda: at(day, hour, minute)


def agent_runner(config: Config, **models: Any) -> AgentRunner:
    return AgentRunner(config.llm, models=models)


# The reviser's revision of the seeded edition's draft, keeping item-1.
REVISION = {
    "headline": "A strong week",
    "draft": {
        "intro": "A good week, tidied up.",
        "sections": DRAFT_V1.model_dump(mode="json")["sections"],
    },
    "item_ids": ["item-1"],
    "changes": ["Tidied the wording"],
    "not_applied": [],
}


def revise_stand_ins(config: Config, **extra_models: Any) -> AgentRunner:
    """The reviser and judge stand-ins for one revision of the seeded edition."""
    return agent_runner(
        config, reviser=scripted([REVISION]), judge=scripted([JUDGE_PASS]), **extra_models
    )


def build_submission() -> FakeMailbox:
    """A submissions mailbox with one pending message, for a non-empty build."""
    return FakeMailbox([message(id="m1", subject="Signed Acme", body="I signed Acme.")])


def build_stand_ins(config: Config, **extra_models: Any) -> AgentRunner:
    """Pipeline agent stand-ins that build one item from `build_submission`'s message."""
    extract = {
        "message_id": "m1",
        "is_update": True,
        "exclusion_reason": None,
        "category": "customer_win",
        "summary": "A win.",
        "facts": ["Signed Acme"],
        "people": ["Priya Shah"],
        "sensitivity": [],
    }
    consolidation = {
        "headline": "A new customer",
        "items": [
            {
                "item_id": "item-1",
                "category": "customer_win",
                "facts": ["Signed Acme"],
                "people": ["Priya Shah"],
                "source_message_ids": ["m1"],
            }
        ],
    }
    draft = {
        "intro": "A good week.",
        "sections": [
            {
                "category": "customer_win",
                "entries": [
                    {
                        "item_id": "item-1",
                        "text": "Priya Shah signed Acme.",
                        "people": ["Priya Shah"],
                    }
                ],
            }
        ],
    }
    extra_models.setdefault("judge", scripted([JUDGE_PASS, JUDGE_PASS]))
    return agent_runner(
        config,
        extractor=scripted([extract]),
        consolidator=scripted([consolidation]),
        drafter=scripted([draft]),
        **extra_models,
    )
