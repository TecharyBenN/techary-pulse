import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from pulse.adapters.store import SqliteStore
from pulse.agents.consolidator.agent import build_consolidator
from pulse.agents.extractor.agent import build_extractor
from pulse.agents.judge.agent import build_judge
from pulse.agents.orchestrator.agent import build_agent
from pulse.agents.orchestrator.run import Orchestrator
from pulse.agents.orchestrator.tools import PROGRESS_NOTES, ShowResult, Tools, progress_note
from pulse.agents.writer.agent import build_writer
from pulse.entities.content import Entry, JudgeOutput
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.lifecycle import start_send
from tests.emails import (
    CORPUS_ITEM_SOURCES,
    CORPUS_OUTCOMES,
    corpus_consolidation,
    corpus_email,
    corpus_messages,
    make_consolidation,
    make_consolidator_item,
    make_consolidator_output,
    make_draft,
    make_email,
    make_item,
    make_output,
    make_verdict,
    stored_outcomes,
)
from tests.fakes.mailbox import FakeMailbox
from tests.fakes.models import extractor_model, reply_with, responses, text_response
from tests.messages import REVIEWER, make_message
from tests.operations import REVIEWERS, make_operations

pytestmark = pytest.mark.anyio

CATEGORIES = {"customer_win": "A new customer has signed."}
INBOX = [
    make_email("m01"),
    make_email("m02", body="Nothing to report."),
    make_email("m03", body="Thanks!"),
    make_email("m13", sender_address="alex.morgan@example.com"),
]
# The extractor stand-in finds each email by its body.
OUTPUTS = {
    INBOX[0].body: make_output().model_dump_json(),
    INBOX[1].body: make_output(category=None).model_dump_json(),
    INBOX[2].body: make_output(category=None).model_dump_json(),
}
CONSOLIDATOR_OUTPUT = make_consolidator_output(make_consolidator_item("m01"))
CONSOLIDATION = make_consolidation(make_item(source_message_ids=["m01"]))
DRAFT = make_draft()
JUDGED = JudgeOutput(verdicts=[make_verdict("intro"), make_verdict(claim="Signed two customers")])


@pytest.fixture
async def store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    return store


def _tools(
    store: SqliteStore,
    extractor: FunctionModel | None = None,
    consolidator: FunctionModel | None = None,
    writer: FunctionModel | None = None,
    conversation: FakeMailbox | None = None,
    judge: FunctionModel | None = None,
) -> Tools:
    return Tools(
        make_operations(store, FakeMailbox(INBOX), conversation),
        store,
        build_extractor(extractor or extractor_model(OUTPUTS), CATEGORIES),
        build_consolidator(
            consolidator or FunctionModel(reply_with(CONSOLIDATOR_OUTPUT.model_dump_json())),
            CATEGORIES,
        ),
        build_writer(
            writer or FunctionModel(reply_with(DRAFT.model_dump_json())),
            CATEGORIES,
            {"customer_win": "Customer wins"},
            "Headline of the week",
            400,
        ),
        build_judge(judge or FunctionModel(reply_with(JUDGED.model_dump_json()))),
        CATEGORIES,
    )


async def _extracted(tools: Tools) -> str:
    """Start a newsletter and extract its screened emails, returning the newsletter ID."""
    started = await tools.start_newsletter()
    assert not isinstance(started, str)
    await tools.extract(["m01", "m02", "m03"])
    return started.newsletter_id


async def test_toolset_offers_each_tool(store: SqliteStore) -> None:
    toolset = _tools(store).toolset()

    assert set(toolset.tools) == {
        "start_newsletter",
        "get_newsletter",
        "list_screened_emails",
        "extract",
        "restore",
        "consolidate",
        "get_items",
        "write",
        "get_draft",
        "show_draft",
        "check",
        "judge",
        "present_draft",
    }


async def test_every_tool_has_a_progress_note(store: SqliteStore) -> None:
    assert set(PROGRESS_NOTES) == set(_tools(store).toolset().tools)
    assert progress_note("get_newsletter") == "Checking the current newsletter (get_newsletter)"


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
        "items": 0,
        # m01 is included but not yet consolidated.
        "items_up_to_date": False,
        "excluded_records": 2,
        "draft_changed": False,
    }


async def test_list_screened_emails_never_returns_subjects_or_bodies(store: SqliteStore) -> None:
    tools = _tools(store)
    await tools.start_newsletter()
    await tools.extract(["m01"])

    listed = await tools.list_screened_emails()

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

    assert await tools.list_screened_emails() == "Refused: no newsletter is open"
    assert await tools.extract(["m01"]) == "Refused: no newsletter is open"
    assert await tools.consolidate(["m01"]) == "Refused: no newsletter is open"
    assert await tools.get_items() == "Refused: no newsletter is open"
    assert await tools.write("Write the first draft.") == "Refused: no newsletter is open"
    assert await tools.get_draft() == "Refused: no newsletter is open"
    assert await tools.present_draft() == "Refused: no newsletter is open"


async def test_extract_stores_each_record_with_its_exclusion(store: SqliteStore) -> None:
    tools = _tools(store)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    result = await tools.extract(["m01", "m02", "m03"])

    assert not isinstance(result, str)
    assert (result.included, result.excluded, result.failed) == (
        1,
        {"no_category": 2},
        0,
    )
    outcomes = result.outcomes
    assert [(o.message_id, o.exclusion, o.error) for o in outcomes] == [
        ("m01", None, None),
        ("m02", "no_category", None),
        ("m03", "no_category", None),
    ]
    # Parallel extractions are numbered in the order they finish.
    assert outcomes[0].excluded_id is None
    assert {outcomes[1].excluded_id, outcomes[2].excluded_id} == {"excluded-1", "excluded-2"}
    records = await store.list_extract_records(started.newsletter_id)
    assert {r.message_id for r in records} == {"m01", "m02", "m03"}


async def test_extract_refuses_rejected_and_unknown_emails(store: SqliteStore) -> None:
    tools = _tools(store)
    await tools.start_newsletter()

    result = await tools.extract(["m13", "m99"])

    assert not isinstance(result, str)
    assert (result.included, result.excluded, result.failed) == (0, {}, 2)
    assert [(o.message_id, o.error) for o in result.outcomes] == [
        ("m13", "the pre-filter rejected it: sender_domain"),
        ("m99", "not a screened email of the open newsletter"),
    ]


async def test_extract_reports_an_invalid_response_and_stores_nothing(store: SqliteStore) -> None:
    invalid = {INBOX[0].body: make_output(category="gossip").model_dump_json()}
    tools = _tools(store, extractor_model(invalid))
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    result = await tools.extract(["m01"])

    assert not isinstance(result, str)
    assert result.failed == 1
    [outcome] = result.outcomes
    assert outcome.error is not None
    assert "gossip is not a configured category" in outcome.error
    assert await store.list_extract_records(started.newsletter_id) == []


async def test_extract_again_replaces_the_record(store: SqliteStore) -> None:
    tools = _tools(store)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)
    await tools.extract(["m02"])

    await tools.extract(["m02"])

    [record] = await store.list_extract_records(started.newsletter_id)
    assert record.excluded_id == "excluded-1"


async def test_corpus_starts_and_extracts_as_the_corpus_expects(store: SqliteStore) -> None:
    messages = corpus_messages()
    inbox = [corpus_email(message, n) for n, message in enumerate(messages)]
    outputs = {m["body"]: json.dumps(m["extract"]) for m in messages if "extract" in m}
    categories = {
        "customer_win": "A new customer has signed.",
        "delivery_highlight": "A project has been delivered.",
        "team_news": "Someone has joined.",
        "shout_out": "A colleague is thanked.",
    }
    consolidator = FunctionModel(reply_with(corpus_consolidation().model_dump_json()))
    tools = Tools(
        make_operations(store, FakeMailbox(inbox)),
        store,
        build_extractor(extractor_model(outputs), categories),
        build_consolidator(consolidator, categories),
        build_writer(
            FunctionModel(reply_with(DRAFT.model_dump_json())),
            categories,
            {"customer_win": "Customer wins"},
            "Headline of the week",
            400,
        ),
        build_judge(FunctionModel(reply_with(JUDGED.model_dump_json()))),
        categories,
    )
    passed = [m for m, outcome in CORPUS_OUTCOMES.items() if not outcome.startswith("rejected")]
    included = [m for m, outcome in CORPUS_OUTCOMES.items() if outcome == "included"]
    model = responses(
        _call("get_newsletter"),
        _call("start_newsletter"),
        _call("list_screened_emails"),
        _call("extract", {"message_ids": passed}),
        _call("consolidate", {"message_ids": included}),
        text_response("Done."),
    )
    orchestrator = Orchestrator(
        build_agent(FunctionModel(model), tools.toolset()), store, 40, timedelta(minutes=15)
    )

    reply = await orchestrator.handle(make_message(text="Please draft a newsletter."))
    assert reply.text == "Done."

    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    assert await stored_outcomes(store, newsletter.newsletter_id) == CORPUS_OUTCOMES
    records = await store.list_extract_records(newsletter.newsletter_id)
    excluded_ids = sorted(r.excluded_id for r in records if r.excluded_id)
    assert excluded_ids == sorted(f"excluded-{n}" for n in range(1, 10))
    items = await store.get_items(newsletter.newsletter_id)
    assert items is not None
    assert [item.source_message_ids for item in items.items] == CORPUS_ITEM_SOURCES


async def test_consolidate_stores_the_items_and_headline(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _extracted(tools)

    result = await tools.consolidate(["m01"])

    assert result == {
        "headline": "A new retail customer",
        "item_count": 1,
        "items": [{"item_id": "item-1", "category": "customer_win", "source_message_ids": ["m01"]}],
    }
    assert await store.get_items(newsletter_id) == CONSOLIDATION
    newsletter = await tools.get_newsletter()
    assert isinstance(newsletter, dict)
    assert (newsletter["items"], newsletter["items_up_to_date"]) == (1, True)


async def test_consolidate_refuses_excluded_records(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _extracted(tools)

    result = await tools.consolidate(["m01", "m02"])

    assert isinstance(result, str)
    assert result.startswith("Refused: m02 is excluded as excluded-")
    assert await store.get_items(newsletter_id) is None


async def test_consolidate_reports_an_invalid_response_and_stores_nothing(
    store: SqliteStore,
) -> None:
    invalid = make_consolidator_output(make_consolidator_item("m01", "m09")).model_dump_json()
    consolidator = FunctionModel(responses(text_response(invalid), text_response(invalid)))
    tools = _tools(store, consolidator=consolidator)
    newsletter_id = await _extracted(tools)

    result = await tools.consolidate(["m01"])

    assert result == (
        "Failed: consolidator gave no valid response: source m09 is not an input record"
    )
    assert await store.get_items(newsletter_id) is None


async def test_get_items_before_consolidation(store: SqliteStore) -> None:
    tools = _tools(store)
    await _extracted(tools)

    items = await tools.get_items()

    assert isinstance(items, dict)
    assert (items["headline"], items["items"]) == (None, [])
    assert {r["message_id"] for r in items["excluded_records"]} == {"m02", "m03"}


async def test_get_items_adds_sources_and_never_returns_subjects_or_bodies(
    store: SqliteStore,
) -> None:
    tools = _tools(store)
    await _extracted(tools)
    await tools.consolidate(["m01"])

    items = await tools.get_items()

    assert isinstance(items, dict)
    assert items["headline"] == "A new retail customer"
    [item] = items["items"]
    assert (item["item_id"], item["sender_names"], item["received"]) == (
        "item-1",
        ["Priya Shah"],
        ["2026-09-22T15:30:00Z"],
    )
    text = json.dumps(items)
    assert "Signed Northwind Retail today" not in text
    assert "Nothing to report." not in text


async def _consolidated(tools: Tools) -> str:
    newsletter_id = await _extracted(tools)
    await tools.consolidate(["m01"])
    return newsletter_id


def _writer_prompts(prompts: list[str]) -> FunctionModel:
    """The writer stand-in, recording each prompt it is given."""

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        prompts.extend(
            p.content
            for p in request.parts
            if isinstance(p, UserPromptPart) and isinstance(p.content, str)
        )
        return text_response(DRAFT.model_dump_json())

    return FunctionModel(model)


def _block(prompt: str, tag: str) -> object:
    return json.loads(prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0])


async def test_write_stores_the_first_draft(store: SqliteStore) -> None:
    prompts: list[str] = []
    tools = _tools(store, writer=_writer_prompts(prompts))
    newsletter_id = await _consolidated(tools)

    result = await tools.write("Write the first draft.")

    assert result == {
        "changes": [],
        "not_applied": [],
        "item_ids": ["item-1"],
        "draft_changed": True,
    }
    assert await store.get_draft(newsletter_id) == DRAFT
    [prompt] = prompts
    assert _block(prompt, "instruction") == "Write the first draft."
    assert "<working_draft>" not in prompt


async def test_write_revises_the_working_draft_with_all_feedback(store: SqliteStore) -> None:
    prompts: list[str] = []
    tools = _tools(store, writer=_writer_prompts(prompts))
    newsletter_id = await _consolidated(tools)
    await store.record_feedback(newsletter_id, make_message("r01", text="Shorter intro, please."))
    await tools.write("Write the first draft.")

    await tools.present_draft()

    revised = await tools.write("Shorten the intro.")

    # The stand-in returns the same draft again, so it has not changed since v1.
    assert isinstance(revised, dict)
    assert revised["draft_changed"] is False
    assert _block(prompts[-1], "working_draft") == DRAFT.content.model_dump(mode="json")
    assert _block(prompts[-1], "feedback") == [
        {"received": "2026-09-26T10:00:00Z", "text": "Shorter intro, please."}
    ]


async def test_write_reports_an_invalid_response_and_stores_nothing(store: SqliteStore) -> None:
    invalid = make_draft(Entry(item_id="item-9", text="Unknown.", people=[])).model_dump_json()
    tools = _tools(store, writer=FunctionModel(reply_with(invalid)))
    newsletter_id = await _consolidated(tools)

    result = await tools.write("Write the first draft.")

    assert result == (
        "Failed: writer gave no valid response: item-9 in item_ids is not a known item"
    )
    assert await store.get_draft(newsletter_id) is None


async def test_get_draft_returns_the_working_draft_or_a_version(store: SqliteStore) -> None:
    tools = _tools(store)
    await _consolidated(tools)
    assert await tools.get_draft() == "Refused: there is no working draft"
    await tools.write("Write the first draft.")
    await tools.present_draft()

    assert await tools.get_draft() == DRAFT.model_dump(mode="json")
    version = await tools.get_draft(1)
    assert isinstance(version, dict)
    assert (version["version"], version["content"]) == (1, DRAFT.content.model_dump(mode="json"))
    assert await tools.get_draft(2) == "Refused: v2 has not been presented"


async def test_show_draft_names_what_to_show_and_refuses_what_does_not_exist(
    store: SqliteStore,
) -> None:
    tools = _tools(store)
    await _consolidated(tools)
    assert await tools.show_draft() == "Refused: there is no working draft"
    await tools.write("Write the first draft.")
    await tools.present_draft()

    assert await tools.show_draft() == ShowResult(version=None)
    assert await tools.show_draft(1) == ShowResult(version=1)
    assert await tools.show_draft(2) == "Refused: v2 has not been presented"


async def test_present_draft_emails_the_next_version(store: SqliteStore) -> None:
    conversation = FakeMailbox()
    tools = _tools(store, conversation=conversation)
    await _consolidated(tools)
    assert await tools.present_draft() == "Refused: there is no working draft"
    await tools.write("Write the first draft.")

    result = await tools.present_draft()

    assert not isinstance(result, str)
    assert result.version == 1
    [email] = conversation.sent
    assert email.to == [REVIEWERS]


async def test_get_newsletter_shows_whether_the_draft_changed(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _consolidated(tools)

    async def draft_changed() -> object:
        summary = await tools.get_newsletter()
        assert isinstance(summary, dict)
        return summary["draft_changed"]

    assert await draft_changed() is False
    await tools.write("Write the first draft.")
    assert await draft_changed() is True
    await tools.present_draft()
    assert await draft_changed() is False
    shorter = DRAFT.content.model_copy(update={"intro": "A shorter intro."})
    await store.save_draft(newsletter_id, DRAFT.model_copy(update={"content": shorter}))
    assert await draft_changed() is True


async def test_check_returns_every_failure_of_the_working_draft(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _consolidated(tools)
    assert await tools.check() == "Refused: there is no working draft"
    dashed = DRAFT.content.model_copy(update={"intro": "A strong week \N{EN DASH} again."})
    await store.save_draft(newsletter_id, DRAFT.model_copy(update={"content": dashed}))

    assert await tools.check() == {
        "failure_count": 1,
        "failures": [{"check": "dashes", "target": "intro", "detail": "em or en dash"}],
    }


async def test_judge_stores_its_verdicts_and_returns_the_unsupported_claims(
    store: SqliteStore,
) -> None:
    tools = _tools(store)
    newsletter_id = await _consolidated(tools)
    assert await tools.judge() == "Refused: there is no working draft"
    await tools.write("Write the first draft.")

    result = await tools.judge()

    assert result == {
        "judged": 2,
        "unsupported": [{"target": "item-1", "claim": "Signed two customers"}],
    }
    assert await store.get_verdicts(newsletter_id) == JUDGED.verdicts


async def test_judge_reports_an_invalid_response_and_stores_nothing(store: SqliteStore) -> None:
    invalid = JudgeOutput(verdicts=[make_verdict("intro")]).model_dump_json()
    tools = _tools(store, judge=FunctionModel(reply_with(invalid)))
    newsletter_id = await _consolidated(tools)
    await tools.write("Write the first draft.")

    assert await tools.judge() == "Failed: judge gave no valid response: item-1 has 0 verdicts"
    assert await store.get_verdicts(newsletter_id) is None


def _run_for(message: ReviewerMessage) -> RunContext[ReviewerMessage]:
    """The context a run gives its tools: the reviewer message it answers."""
    return RunContext(deps=message, model=TestModel(), usage=RunUsage())


async def _excluded_id(store: SqliteStore, newsletter_id: str, message_id: str) -> str:
    records = await store.list_extract_records(newsletter_id)
    [record] = [r for r in records if r.message_id == message_id]
    assert record.excluded_id is not None
    return record.excluded_id


async def test_restore_includes_the_record_for_the_reviewer(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _consolidated(tools)
    excluded_id = await _excluded_id(store, newsletter_id, "m02")

    result = await tools.restore(_run_for(make_message()), excluded_id)

    assert result == {"excluded_id": excluded_id, "message_id": "m02"}
    records = {r.message_id: r for r in await store.list_extract_records(newsletter_id)}
    assert (records["m02"].exclusion, records["m02"].restored_by) == (None, REVIEWER)
    # The restored record is not in the items until consolidate runs again.
    summary = await tools.get_newsletter()
    assert isinstance(summary, dict)
    assert summary["items_up_to_date"] is False


async def test_restore_refuses_an_id_that_names_no_excluded_record(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _consolidated(tools)
    excluded_id = await _excluded_id(store, newsletter_id, "m02")
    await tools.restore(_run_for(make_message()), excluded_id)

    assert await tools.restore(_run_for(make_message()), excluded_id) == (
        f"Refused: {excluded_id} names no excluded record in the open newsletter"
    )


async def test_restore_refuses_without_an_open_newsletter(store: SqliteStore) -> None:
    assert await _tools(store).restore(_run_for(make_message()), "excluded-1") == (
        "Refused: no newsletter is open"
    )


def _call(tool: str, args: dict[str, object] | None = None) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool, args or {})])
