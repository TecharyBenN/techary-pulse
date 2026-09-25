import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.runner import AgentRunner
from pulse.config import Config
from pulse.errors import AgentResponseError, GatewayError, LockHeldError, StopRequested
from pulse.pipeline.runner import run_pipeline
from pulse.state import run_lock

from ..support import FakeMailbox, MailboxFailure, answering, scripted
from .conftest import CORPUS, clock, corpus_messages, draft, stand_ins

Runner = Callable[..., AgentRunner]
GOLDEN = Path(__file__).parent.parent / "golden" / "reviewer_email.html"
INCLUDED = {"m01", "m02", "m03", "m04", "m05", "m06"}
EXCLUDED = {"m08", "m10", "m11", "m12", "m15", "m16", "m18"}
REJECTED = {"m07", "m09", "m13", "m14", "m17"}


def test_corpus_run_sends_one_draft_and_moves_every_message(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    result = run_pipeline(config, mailbox, runner(), clock())

    assert result.status == "sent"
    assert len(mailbox.sent) == 1
    email = mailbox.sent[0]
    assert email.to == config.reviewers and email.reply_to == config.reviewers
    assert email.subject == "Pulse: week ending 25 September 2026"
    assert mailbox.inbox == {}
    assert set(mailbox.folders["Processed"]) == INCLUDED | EXCLUDED
    assert set(mailbox.folders["Rejected"]) == REJECTED
    assert result.manifest.complete and result.manifest.sent
    assert {i for i, o in result.manifest.outcomes.items() if o.status == "rejected"} == REJECTED
    for name in (
        "messages.json",
        "extract.json",
        "consolidate.json",
        "draft-1.json",
        "checks-1.json",
        "reviewer-email.html",
        "manifest.json",
    ):
        assert (result.artefacts_dir / name).exists(), name


def test_corpus_email_matches_golden_file(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    run_pipeline(config, mailbox, runner(), clock())
    assert mailbox.sent[0].html == GOLDEN.read_text(encoding="utf-8")


def test_review_section_lists_exclusions_rejections_attachments_and_sources(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    run_pipeline(config, mailbox, runner(), clock())
    html = mailbox.sent[0].html
    assert "commercial (mentions annual contract value)" in html
    assert "inappropriate (criticises a named customer contact)" in html
    assert "unclear" in html and "no_matching_section" in html and "not_an_update" in html
    assert "Partnership opportunity" in html and "Board pack notes" in html
    assert 'Ben Carter, "New joiner on the service desk"' in html
    assert 'Tom Evans, "Northwind deal closed"' in html


def test_empty_week_sends_nothing_and_moves_only_rejected(config: Config, runner: Runner) -> None:
    mailbox = FakeMailbox(corpus_messages(EXCLUDED | REJECTED))
    result = run_pipeline(config, mailbox, runner(), clock())
    assert result.status == "empty"
    assert mailbox.sent == []
    assert set(mailbox.folders["Rejected"]) == REJECTED
    assert set(mailbox.inbox) == EXCLUDED


def test_dry_run_sends_and_moves_nothing_but_saves_the_email(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    result = run_pipeline(config, mailbox, runner(), clock(), dry_run=True)
    assert result.status == "dry_run" and result.manifest.dry_run
    assert mailbox.sent == [] and mailbox.folders == {}
    assert (result.artefacts_dir / "reviewer-email.html").exists()

    run_pipeline(config, mailbox, runner(), clock(minute=31))
    assert len(mailbox.sent) == 1


def test_failed_draft_is_regenerated_once_with_the_reasons(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    bad = draft(intro="Seven new customers.")
    bad["intro"] = "A good week with 7 wins."
    run_pipeline(config, mailbox, runner(drafts=[bad, CORPUS["draft"]]), clock())
    assert len(mailbox.sent) == 1
    assert "Check failures" not in mailbox.sent[0].html


def test_second_failing_draft_is_sent_with_failures_first(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    bad = draft(intro="A good week with 7 wins.")
    run_pipeline(config, mailbox, runner(drafts=[bad, bad]), clock())
    html = mailbox.sent[0].html
    assert "Check failures" in html and "intro number 7" in html
    assert html.index("Check failures") < html.index("Excluded for sensitivity")


def test_invalid_response_twice_fails_the_run_with_nothing_sent_or_moved(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    with pytest.raises(AgentResponseError) as error:
        run_pipeline(config, mailbox, runner(extractor=scripted(["not json"] * 50)), clock())
    assert error.value.agent == "extractor"
    assert mailbox.sent == [] and mailbox.folders == {}


def test_gateway_error_fails_the_run_with_nothing_sent_or_moved(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=504, model_name="m")

    with pytest.raises(GatewayError):
        run_pipeline(config, mailbox, runner(consolidator=FunctionModel(fail)), clock())
    assert mailbox.sent == [] and mailbox.folders == {}


def test_send_failure_moves_nothing_and_the_next_run_processes_the_same_messages(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    mailbox.fail_send = True
    with pytest.raises(MailboxFailure):
        run_pipeline(config, mailbox, runner(), clock())
    assert mailbox.folders == {}

    mailbox.fail_send = False
    run_pipeline(config, mailbox, runner(), clock(minute=31))
    assert len(mailbox.sent) == 1 and mailbox.inbox == {}


def test_move_failure_is_completed_by_the_next_run_before_anything_else(
    config: Config, mailbox: FakeMailbox, runner: Runner
) -> None:
    mailbox.fail_move_after = 3
    with pytest.raises(MailboxFailure):
        run_pipeline(config, mailbox, runner(), clock())
    assert len(mailbox.sent) == 1 and len(mailbox.inbox) == 15

    mailbox.fail_move_after = None
    result = run_pipeline(config, mailbox, runner(), clock(minute=31))
    assert len(mailbox.sent) == 1, "the draft must not be sent twice"
    assert mailbox.inbox == {} and result.status == "empty"


BOUNDARIES = [
    "lock and resume",
    "snapshot",
    "pre-filter",
    "extract",
    "consolidate",
    "check",
    "render and send",
]


@pytest.mark.parametrize("stop_after", BOUNDARIES)
def test_stop_at_each_step_boundary_then_recover(
    config: Config, mailbox: FakeMailbox, runner: Runner, stop_after: str
) -> None:
    reached: list[str] = []

    def should_stop() -> bool:
        path = max(config.run_artefacts_dir.glob("*/manifest.json"))
        manifest_step = str(json.loads(path.read_text())["step"])
        reached.append(manifest_step)
        return manifest_step == stop_after

    with pytest.raises(StopRequested):
        run_pipeline(config, mailbox, runner(), clock(), should_stop=should_stop)
    sent_before = len(mailbox.sent)
    assert sent_before == (1 if stop_after == "render and send" else 0)
    assert mailbox.folders == {}

    run_pipeline(config, mailbox, runner(), clock(minute=31))
    assert len(mailbox.sent) == 1, "exactly one draft across both runs"
    assert mailbox.inbox == {}


def test_second_concurrent_run_exits(config: Config, mailbox: FakeMailbox, runner: Runner) -> None:
    with run_lock(config.run_artefacts_dir / "pulse.lock"), pytest.raises(LockHeldError):
        run_pipeline(config, mailbox, runner(), clock())


def test_regeneration_receives_the_failure_reasons(config: Config, mailbox: FakeMailbox) -> None:
    prompts: list[str] = []
    responses = iter([draft(intro="A good week with 7 wins."), CORPUS["draft"]])

    def respond(prompt: str) -> object:
        prompts.append(prompt)
        return next(responses)

    models = stand_ins()
    models["drafter"] = answering(respond)
    run_pipeline(config, mailbox, AgentRunner(config.llm, models=models), clock())
    assert '"failures": []' in prompts[0]
    assert "intro number 7" in prompts[1]
