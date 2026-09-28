"""Runs the synthetic emails through the real agents on the dev gateway.

Takes the gateway settings (`llm`) and `run_artefacts_dir` from `config.yaml`, with the credential
in `.env`, as the README's local setup describes. Every other setting comes from the example
configuration the synthetic emails are written for. It is a dry run against the fake mailbox, so
nothing is sent or moved; the reviewer email is saved in the run's artefacts for a reviewer to read.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pulse.agents.runner import AgentRunner
from pulse.config import load_config
from pulse.pipeline.runner import run_build
from pulse.store import EditionStore

from ..support import FakeMailbox
from .conftest import corpus_messages

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.live
@pytest.mark.anyio
async def test_corpus_through_the_dev_gateway(tmp_path: Path) -> None:
    local = load_config(ROOT / "config.yaml")
    config = load_config(ROOT / "config" / "config.example.yaml").model_copy(
        update={"llm": local.llm, "run_artefacts_dir": local.run_artefacts_dir}
    )
    result = await run_build(
        config,
        FakeMailbox(corpus_messages()),
        FakeMailbox([]),
        EditionStore(tmp_path / "pulse.db"),
        AgentRunner(config.llm),
        lambda: datetime.now(UTC),
        "command",
        dry_run=True,
    )
    assert result.artefacts_dir is not None
    email = result.artefacts_dir / "reviewer-email.html"
    print(f"\nReviewer email: {email}")
    assert result.status == "dry_run"
    assert email.exists()
