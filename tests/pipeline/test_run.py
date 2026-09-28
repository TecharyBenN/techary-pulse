import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents.runner import AgentRunner
from pulse.config import Config
from pulse.errors import AgentResponseError, GatewayError, StopRequested
from pulse.pipeline.runner import run_build
from pulse.state import run_lock
from pulse.store import EditionStore

from ..support import FakeMailbox, MailboxFailure, answering, scripted
from .conftest import CORPUS, clock, corpus_messages, draft, stand_ins

pytestmark = pytest.mark.anyio

Runner = Callable[..., AgentRunner]
GOLDEN = Path(__file__).parent.parent / "golden" / "reviewer_email.html"
INCLUDED = {"m01", "m02", "m03", "m04", "m05", "m06"}
EXCLUDED = {"m08", "m09", "m10", "m11", "m12", "m15", "m16", "m17", "m18"}
REJECTED = {"m07", "m13", "m14"}


async def test_corpus_build_creates_an_edition_and_moves_every_message(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    db_path: Path,
    runner: Runner,
) -> None:
    result = await run_build(config, submissions, conversation, store, runner(), clock(), "command")

    assert result.status == "created"
    assert result.edition is not None
    assert len(conversation.sent) == 1
    email = conversation.sent[0]
    assert email.to == config.reviewers and email.reply_to == []
    assert email.subject == "Draft v1: Pulse: 25 September 2026"
    assert submissions.inbox == {}
    assert set(submissions.folders["Processed"]) == INCLUDED | EXCLUDED
    assert set(submissions.folders["Rejected"]) == REJECTED
    assert result.manifest is not None and result.manifest.complete
    assert {i for i, o in result.manifest.outcomes.items() if o.status == "rejected"} == REJECTED

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT item_id, kind FROM items WHERE edition_id = ?", (result.edition.id,)
    ).fetchall()
    assert {r[0] for r in rows if r[1] == "item"}
    assert {r[0] for r in rows if r[1] == "excluded"} == {
        f"excluded-{n}" for n in range(1, len(EXCLUDED) + 1)
    }
    version = conn.execute(
        "SELECT version, creator FROM versions WHERE edition_id = ?", (result.edition.id,)
    ).fetchone()
    assert version == (1, "pulse")
    note = conn.execute(
        "SELECT payload FROM messages WHERE edition_id = ?", (result.edition.id,)
    ).fetchone()
    assert note is not None and "Pulse note: version 1 was sent" in note[0]

    assert result.artefacts_dir is not None
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


async def test_corpus_email_matches_golden_file(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    await run_build(config, submissions, conversation, store, runner(), clock(), "command")
    assert conversation.sent[0].html == GOLDEN.read_text(encoding="utf-8")


async def test_review_section_lists_exclusions_rejections_attachments_and_sources(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    await run_build(config, submissions, conversation, store, runner(), clock(), "command")
    html = conversation.sent[0].html
    assert "commercial (mentions annual contract value)" in html
    assert "inappropriate (criticises a named customer contact)" in html
    assert "unclear" in html and "no_matching_section" in html and "not_an_update" in html
    assert "Partnership opportunity" in html and "Board pack notes" in html
    assert 'Ben Carter, "New joiner on the service desk"' in html
    assert 'Tom Evans, "Northwind deal closed"' in html


async def test_second_request_returns_the_open_edition_and_sends_nothing(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    first = await run_build(config, submissions, conversation, store, runner(), clock(), "command")

    result = await run_build(
        config, submissions, conversation, store, runner(), clock(minute=31), "command"
    )
    assert result.status == "open"
    assert result.edition == first.edition
    assert len(conversation.sent) == 1


async def test_held_lock_returns_build_in_progress(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    with run_lock(config.run_artefacts_dir / "build.lock"):
        result = await run_build(
            config, submissions, conversation, store, runner(), clock(), "command"
        )
    assert result.status == "in_progress"
    assert result.edition is None


async def test_empty_build_sends_nothing_and_moves_only_rejected(
    config: Config, conversation: FakeMailbox, store: EditionStore, runner: Runner
) -> None:
    submissions = FakeMailbox(corpus_messages(EXCLUDED | REJECTED))
    result = await run_build(config, submissions, conversation, store, runner(), clock(), "command")
    assert result.status == "empty" and result.edition is None
    assert conversation.sent == []
    assert set(submissions.folders["Rejected"]) == REJECTED
    assert set(submissions.inbox) == EXCLUDED
    assert await store.open_edition() is None


async def test_dry_run_sends_moves_and_saves_nothing_but_keeps_the_email(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    result = await run_build(
        config, submissions, conversation, store, runner(), clock(), "command", dry_run=True
    )
    assert result.status == "dry_run" and result.edition is None
    assert result.manifest is not None and result.manifest.dry_run
    assert conversation.sent == [] and submissions.folders == {}
    assert result.artefacts_dir is not None
    assert (result.artefacts_dir / "reviewer-email.html").exists()
    assert await store.open_edition() is None

    result = await run_build(
        config, submissions, conversation, store, runner(), clock(minute=31), "command"
    )
    assert result.status == "created"
    assert len(conversation.sent) == 1


async def test_failed_draft_is_regenerated_once_with_the_reasons(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    bad = draft(intro="Seven new customers.")
    bad["intro"] = "A good week with 7 wins."
    await run_build(
        config,
        submissions,
        conversation,
        store,
        runner(drafts=[bad, CORPUS["draft"]]),
        clock(),
        "command",
    )
    assert len(conversation.sent) == 1
    assert "Check failures" not in conversation.sent[0].html


async def test_second_failing_draft_is_sent_with_failures_first(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    bad = draft(intro="A good week with 7 wins.")
    await run_build(
        config, submissions, conversation, store, runner(drafts=[bad, bad]), clock(), "command"
    )
    html = conversation.sent[0].html
    assert "Check failures" in html and "intro number 7" in html
    assert html.index("Check failures") < html.index("Excluded for sensitivity")


async def test_invalid_response_twice_fails_the_build_with_nothing_sent_or_moved(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    with pytest.raises(AgentResponseError) as error:
        await run_build(
            config,
            submissions,
            conversation,
            store,
            runner(extractor=scripted(["not json"] * 50)),
            clock(),
            "command",
        )
    assert error.value.agent == "extractor"
    assert conversation.sent == [] and submissions.folders == {}
    assert await store.open_edition() is None


async def test_gateway_error_fails_the_build_with_nothing_sent_or_moved(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=504, model_name="m")

    with pytest.raises(GatewayError):
        await run_build(
            config,
            submissions,
            conversation,
            store,
            runner(consolidator=FunctionModel(fail)),
            clock(),
            "command",
        )
    assert conversation.sent == [] and submissions.folders == {}
    assert await store.open_edition() is None


async def test_send_failure_leaves_no_edition_and_the_next_build_processes_the_same_messages(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    conversation.fail_send = True
    with pytest.raises(MailboxFailure):
        await run_build(config, submissions, conversation, store, runner(), clock(), "command")
    assert submissions.folders == {}
    assert await store.open_edition() is None

    conversation.fail_send = False
    result = await run_build(
        config, submissions, conversation, store, runner(), clock(minute=31), "command"
    )
    assert result.status == "created"
    assert len(conversation.sent) == 1 and submissions.inbox == {}


async def test_move_failure_is_completed_by_the_next_build_while_the_edition_is_open(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
) -> None:
    submissions.fail_move_after = 3
    with pytest.raises(MailboxFailure):
        await run_build(config, submissions, conversation, store, runner(), clock(), "command")
    assert len(conversation.sent) == 1 and len(submissions.inbox) == 15
    saved = await store.open_edition()
    assert saved is not None, "the edition must be saved before moves are attempted"

    submissions.fail_move_after = None
    result = await run_build(
        config, submissions, conversation, store, runner(), clock(minute=31), "command"
    )
    assert len(conversation.sent) == 1, "version 1 must not be sent twice"
    assert submissions.inbox == {}
    assert result.status == "open" and result.edition == saved


BOUNDARIES = [
    "lock and resume",
    "snapshot",
    "pre-filter",
    "extract",
    "consolidate",
    "check",
    "send",
]


@pytest.mark.parametrize("stop_after", BOUNDARIES)
async def test_stop_at_each_step_boundary_then_recover(
    config: Config,
    submissions: FakeMailbox,
    conversation: FakeMailbox,
    store: EditionStore,
    runner: Runner,
    stop_after: str,
) -> None:
    def should_stop() -> bool:
        path = max(config.run_artefacts_dir.glob("*/manifest.json"))
        manifest_step = str(json.loads(path.read_text())["step"])
        return manifest_step == stop_after

    with pytest.raises(StopRequested):
        await run_build(
            config,
            submissions,
            conversation,
            store,
            runner(),
            clock(),
            "command",
            should_stop=should_stop,
        )
    assert len(conversation.sent) == (1 if stop_after == "send" else 0)
    assert submissions.folders == {}
    assert await store.open_edition() is None, "the edition is only saved after the send boundary"

    result = await run_build(
        config, submissions, conversation, store, runner(), clock(minute=31), "command"
    )
    assert result.status == "created"
    assert submissions.inbox == {}
    if stop_after == "send":
        assert len(conversation.sent) == 2, "a retry after the send boundary sends a second v1"
    else:
        assert len(conversation.sent) == 1, "exactly one draft across both builds"


async def test_regeneration_receives_the_failure_reasons(
    config: Config, submissions: FakeMailbox, conversation: FakeMailbox, store: EditionStore
) -> None:
    prompts: list[str] = []
    responses = iter([draft(intro="A good week with 7 wins."), CORPUS["draft"]])

    def respond(prompt: str) -> object:
        prompts.append(prompt)
        return next(responses)

    models = stand_ins()
    models["drafter"] = answering(respond)
    await run_build(
        config,
        submissions,
        conversation,
        store,
        AgentRunner(config.llm, models=models),
        clock(),
        "command",
    )
    assert '"failures": []' in prompts[0]
    assert "intro number 7" in prompts[1]
