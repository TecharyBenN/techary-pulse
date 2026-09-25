import copy
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic_ai.models import Model

from pulse.agents.runner import AgentRunner
from pulse.config import Config, load_config
from pulse.models import Message

from ..support import FakeMailbox, answering, scripted

CORPUS = yaml.safe_load((Path(__file__).parent.parent / "corpus" / "corpus.yaml").read_text())
_MESSAGE_ID = re.compile(r'message_id="([^"]+)"')


def corpus_messages(ids: set[str] | None = None) -> list[Message]:
    return [
        Message(
            id=m["id"],
            sender_name=m["sender_name"],
            sender_address=m["sender_address"],
            subject=m["subject"],
            received_at=datetime(2026, 9, 21, 9, n, tzinfo=UTC),
            body=m["body"],
            headers=m.get("headers", {}),
            has_attachments=m.get("has_attachments", False),
        )
        for n, m in enumerate(CORPUS["messages"])
        if ids is None or m["id"] in ids
    ]


def extract_response(prompt: str) -> dict[str, Any]:
    message_id = _MESSAGE_ID.search(prompt).group(1)  # type: ignore[union-attr]
    entry = next(m for m in CORPUS["messages"] if m["id"] == message_id)
    extract: dict[str, Any] = entry["extract"]
    return {"message_id": message_id, **extract}


def draft(**changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = copy.deepcopy(CORPUS["draft"])
    result.update(changes)
    return result


def stand_ins(
    consolidations: list[Any] | None = None,
    drafts: list[Any] | None = None,
    judges: list[Any] | None = None,
    extractor: Model | None = None,
    consolidator: Model | None = None,
) -> dict[str, Model]:
    return {
        "extractor": extractor or answering(extract_response),
        "consolidator": consolidator or scripted(consolidations or [CORPUS["consolidation"]]),
        "drafter": scripted(drafts or [CORPUS["draft"]]),
        "judge": scripted(judges or [CORPUS["judge"]] * 2),
    }


@pytest.fixture
def config(config_dir: Path, tmp_path: Path) -> Config:
    base = load_config(config_dir / "config.example.yaml")
    return base.model_copy(update={"run_artefacts_dir": tmp_path / "runs"})


@pytest.fixture
def mailbox() -> FakeMailbox:
    return FakeMailbox(corpus_messages())


@pytest.fixture
def runner(config: Config) -> Callable[..., AgentRunner]:
    def build(**kwargs: Any) -> AgentRunner:
        return AgentRunner(config.llm, models=stand_ins(**kwargs))

    return build


def clock(day: int = 25, minute: int = 30) -> Callable[[], datetime]:
    return lambda: datetime(2026, 9, day, 17, minute, tzinfo=UTC)
