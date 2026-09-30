"""Runs the orchestrator through the dev gateway, using ./config.yaml and the keys in .env."""

import os
from pathlib import Path

import pytest

from pulse.adapters.clock import SystemClock
from pulse.adapters.store import SqliteStore
from pulse.config import load_config
from pulse.main import build_orchestrator
from tests.fakes.mailbox import FakeMailbox
from tests.messages import make_message, make_newsletter

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_a_reviewer_message_gets_a_reply(tmp_path: Path) -> None:
    config = load_config(Path("config.yaml"))
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    await store.save_start(make_newsletter(), [])
    orchestrator = build_orchestrator(
        config, store, FakeMailbox(), os.environ[config.llm.api_key_env], SystemClock()
    )

    reply = await orchestrator.handle(make_message(text="Hello, what can you do for me?"))

    print(reply)
    assert reply
    assert len(await store.load_history("n-1")) == 2
