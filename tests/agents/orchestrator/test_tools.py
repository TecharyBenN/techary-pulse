import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from pulse.adapters.store import SqliteStore
from pulse.agents.extractor.agent import build_extractor
from pulse.agents.orchestrator.agent import build_agent
from pulse.agents.orchestrator.run import Orchestrator
from pulse.agents.orchestrator.tools import Tools
from pulse.entities.lifecycle import start_send
from pulse.services.operations import Operations
from tests.emails import corpus_email, corpus_messages, make_email, make_output, screen_email
from tests.fakes.clock import ControlledClock
from tests.fakes.mailbox import FakeMailbox
from tests.fakes.models import extractor_model, responses, text_response
from tests.messages import OPENED, make_message

pytestmark = pytest.mark.anyio

CATEGORIES = {"customer_win": "A new customer has signed."}
INBOX = [
    make_email("m01"),
    make_email("m02", body="Nothing to report."),
    make_email("m03", body="Thanks!"),
    make_email("m13", sender_address="alex.morgan@example.com"),
]
OUTPUTS = {
    "m01": make_output("m01").model_dump_json(),
    "m02": make_output("m02", category=None).model_dump_json(),
    "m03": make_output("m03", is_update=False, category=None).model_dump_json(),
}


@pytest.fixture
async def store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    return store


def _tools(store: SqliteStore, extractor: FunctionModel | None = None) -> Tools:
    operations = Operations(store, FakeMailbox(INBOX), screen_email, ControlledClock(OPENED))
    model = extractor or extractor_model(OUTPUTS)
    return Tools(operations, store, build_extractor(model, CATEGORIES), CATEGORIES)


async def test_toolset_offers_each_tool(store: SqliteStore) -> None:
    toolset = _tools(store).toolset()

    assert set(toolset.tools) == {
        "start_newsletter",
        "get_newsletter",
        "list_submissions",
        "extract",
    }


async def test_start_newsletter_reports_what_it_added(store: SqliteStore) -> None:
    result = await _tools(store).start_newsletter()

    assert not isinstance(result, str)
    assert (result.opened, result.added, result.rejected) == (True, 4, 1)


async def test_start_newsletter_refuses_once_the_send_has_started(store: SqliteStore) -> None:
    tools = _tools(store)
    await tools.start_newsletter()
    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    approved = newsletter.model_copy(
        update={"state": "approved", "latest_version": 1, "approved_version": 1}
    )
    await store.save_start(start_send(approved), [])

    assert await tools.start_newsletter() == "Refused: the send has started"


async def test_get_newsletter_without_an_open_newsletter(store: SqliteStore) -> None:
    assert await _tools(store).get_newsletter() == "No newsletter is open."


async def test_get_newsletter_summarises_the_open_newsletter(store: SqliteStore) -> None:
    tools = _tools(store)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)
    await tools.extract(["m01", "m02", "m03"])

    assert await tools.get_newsletter() == {
        "newsletter_id": started.newsletter_id,
        "state": "in_review",
        "latest_version": None,
        "approved_version": None,
        "send_time": None,
        "excluded_records": 2,
    }


async def test_list_submissions_never_returns_subjects_or_bodies(store: SqliteStore) -> None:
    tools = _tools(store)
    await tools.start_newsletter()
    await tools.extract(["m01"])

    listed = await tools.list_submissions()

    assert not isinstance(listed, str)
    assert [(s["message_id"], s["rejection"]) for s in listed] == [
        ("m01", None),
        ("m02", None),
        ("m03", None),
        ("m13", "sender_domain"),
    ]
    assert set(listed[0]) == {
        "message_id",
        "sender_name",
        "received",
        "has_attachments",
        "rejection",
        "extract",
    }
    assert listed[0]["extract"] is not None
    assert listed[1]["extract"] is None
    text = json.dumps(listed)
    assert "Signed Northwind Retail today" not in text
    assert "Nothing to report." not in text


async def test_tools_refuse_without_an_open_newsletter(store: SqliteStore) -> None:
    tools = _tools(store)

    assert await tools.list_submissions() == "Refused: no newsletter is open"
    assert await tools.extract(["m01"]) == "Refused: no newsletter is open"


async def test_extract_stores_each_record_with_its_exclusion(store: SqliteStore) -> None:
    tools = _tools(store)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    outcomes = await tools.extract(["m01", "m02", "m03"])

    assert not isinstance(outcomes, str)
    assert [(o.message_id, o.exclusion, o.error) for o in outcomes] == [
        ("m01", None, None),
        ("m02", "no_matching_section", None),
        ("m03", "not_an_update", None),
    ]
    # Parallel extractions are numbered in the order they finish.
    assert outcomes[0].excluded_id is None
    assert {outcomes[1].excluded_id, outcomes[2].excluded_id} == {"excluded-1", "excluded-2"}
    records = await store.list_extract_records(started.newsletter_id)
    assert {r.message_id for r in records} == {"m01", "m02", "m03"}


async def test_extract_refuses_rejected_and_unknown_submissions(store: SqliteStore) -> None:
    tools = _tools(store)
    await tools.start_newsletter()

    outcomes = await tools.extract(["m13", "m99"])

    assert not isinstance(outcomes, str)
    assert [(o.message_id, o.error) for o in outcomes] == [
        ("m13", "the pre-filter rejected it: sender_domain"),
        ("m99", "not a submission of the open newsletter"),
    ]


async def test_extract_reports_an_invalid_response_and_stores_nothing(store: SqliteStore) -> None:
    invalid = {"m01": make_output("m02").model_dump_json()}
    tools = _tools(store, extractor_model(invalid))
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    outcomes = await tools.extract(["m01"])

    assert not isinstance(outcomes, str)
    [outcome] = outcomes
    assert outcome.error is not None
    assert "not the submission's message ID" in outcome.error
    assert await store.list_extract_records(started.newsletter_id) == []


async def test_extract_again_replaces_the_record(store: SqliteStore) -> None:
    tools = _tools(store)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)
    await tools.extract(["m02"])

    await tools.extract(["m02"])

    [record] = await store.list_extract_records(started.newsletter_id)
    assert record.excluded_id == "excluded-1"


# Written out by hand from the corpus cases, not derived from the rules under test.
CORPUS_OUTCOMES = {
    "m01": "included",
    "m02": "included",
    "m03": "included",
    "m04": "included",
    "m05": "included",
    "m06": "included",
    "m07": "rejected: automatic_reply",
    "m08": "excluded: not_an_update",
    "m09": "excluded: not_an_update",
    "m10": "excluded: not_an_update",
    "m11": "excluded: unclear",
    "m12": "excluded: no_matching_section",
    "m13": "rejected: sender_domain",
    "m14": "rejected: sensitivity_label",
    "m15": "excluded: sensitivity",
    "m16": "excluded: sensitivity",
    "m17": "excluded: not_an_update",
    "m18": "excluded: not_an_update",
}


async def test_corpus_starts_and_extracts_as_the_corpus_expects(store: SqliteStore) -> None:
    messages = corpus_messages()
    inbox = [corpus_email(message, n) for n, message in enumerate(messages)]
    outputs = {
        m["id"]: json.dumps({"message_id": m["id"]} | m["extract"])
        for m in messages
        if "extract" in m
    }
    categories = {
        "customer_win": "A new customer has signed.",
        "delivery_highlight": "A project has been delivered.",
        "team_news": "Someone has joined.",
        "shout_out": "A colleague is thanked.",
    }
    operations = Operations(store, FakeMailbox(inbox), screen_email, ControlledClock(OPENED))
    tools = Tools(
        operations, store, build_extractor(extractor_model(outputs), categories), categories
    )
    passed = [m for m, outcome in CORPUS_OUTCOMES.items() if not outcome.startswith("rejected")]
    model = responses(
        _call("get_newsletter"),
        _call("start_newsletter"),
        _call("list_submissions"),
        _call("extract", {"message_ids": passed}),
        text_response("Done."),
    )
    orchestrator = Orchestrator(
        build_agent(FunctionModel(model), tools.toolset()), store, 40, timedelta(minutes=15)
    )

    assert await orchestrator.handle(make_message(text="Please draft a newsletter.")) == "Done."

    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    submissions = await store.list_submissions(newsletter.newsletter_id)
    records = {r.message_id: r for r in await store.list_extract_records(newsletter.newsletter_id)}
    outcomes = {}
    for submission in submissions:
        record = records.get(submission.message_id)
        if submission.rejection is not None:
            outcomes[submission.message_id] = f"rejected: {submission.rejection}"
        elif record is None:
            outcomes[submission.message_id] = "not extracted"
        elif record.exclusion is None:
            outcomes[submission.message_id] = "included"
        else:
            outcomes[submission.message_id] = f"excluded: {record.exclusion}"
    assert outcomes == CORPUS_OUTCOMES
    excluded_ids = sorted(r.excluded_id for r in records.values() if r.excluded_id)
    assert excluded_ids == sorted(f"excluded-{n}" for n in range(1, 10))


def _call(tool: str, args: dict[str, object] | None = None) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool, args or {})])
