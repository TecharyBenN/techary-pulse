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
from pulse.pipeline.runner import run_pipeline

from ..support import FakeMailbox
from .conftest import corpus_messages

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.live
def test_corpus_through_the_dev_gateway() -> None:
    local = load_config(ROOT / "config.yaml")
    config = load_config(ROOT / "config" / "config.example.yaml").model_copy(
        update={"llm": local.llm, "run_artefacts_dir": local.run_artefacts_dir}
    )
    result = run_pipeline(
        config,
        FakeMailbox(corpus_messages()),
        AgentRunner(config.llm),
        lambda: datetime.now(UTC),
        dry_run=True,
    )
    email = result.artefacts_dir / "reviewer-email.html"
    print(f"\nReviewer email: {email}")
    assert result.status == "dry_run"
    assert email.exists()
