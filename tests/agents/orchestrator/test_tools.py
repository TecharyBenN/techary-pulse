import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelHTTPError
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
from pulse.agents.consolidator.agent import ConsolidatorAgent
from pulse.agents.decomposer.agent import DecomposerAgent, DecomposerTask
from pulse.agents.extractor.agent import ExtractorAgent
from pulse.agents.judge.agent import JudgeAgent
from pulse.agents.orchestrator.tools import (
    PROGRESS_NOTES,
    ShowResult,
    Tools,
    extract_news,
    progress_note,
)
from pulse.agents.sensitivity.agent import SensitivityAgent
from pulse.agents.writer.agent import WriterAgent
from pulse.entities.content import Entry, JudgeOutput
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.extracts import DecomposerOutput, Extraction, ExtractorOutput, LineFacts
from pulse.entities.lifecycle import abandon, start_send
from pulse.entities.mail import ScreenedEmail, message_lines
from pulse.services.operations import ApproveResult, NoticeResult, RestoreResult
from tests.emails import (
    CORPUS_ITEM_SOURCES,
    CORPUS_OUTCOMES,
    ENTRY,
    corpus_email,
    corpus_messages,
    make_consolidation,
    make_consolidator_item,
    make_consolidator_output,
    make_draft,
    make_email,
    make_item,
    make_output,
    make_screened_email,
    make_verdict,
    stored_outcomes,
)
from tests.fakes.mailbox import FakeMailbox
from tests.fakes.models import (
    corpus_consolidator_model,
    data_block,
    decomposer_model,
    extractor_model,
    first_prompt,
    gateway_error,
    make_orchestrator,
    reply_with,
    responses,
    sensitivity_model,
    text_response,
)
from tests.messages import OPENED, REVIEWER, make_message, make_newsletter
from tests.operations import REVIEWERS, make_operations

pytestmark = pytest.mark.anyio

CATEGORIES = {"customer_win": "A new customer has signed."}
INBOX = [
    make_email("m01"),
    make_email("m02", body="Nothing to report."),
    make_email("m03", body="Thanks!"),
    make_email("m13", sender_address="alex.morgan@example.com"),
]
# The extractor and sensitivity stand-ins find each email by its body.
OUTPUTS = {
    INBOX[0].body: make_output(),
    # Every number in an extractor's facts must be in its email, so these facts give none.
    INBOX[1].body: make_output(category=None, facts=["The team has nothing new this week."]),
    INBOX[2].body: make_output(category=None, facts=["A colleague said thank you."]),
}
CONSOLIDATOR_OUTPUT = make_consolidator_output(make_consolidator_item("record-1"))
CONSOLIDATION = make_consolidation(make_item(source_message_ids=["m01"]))
DRAFT = make_draft()
JUDGED = [make_verdict("intro"), make_verdict(claim="Signed two customers")]


def judge_model(seen: list[dict[str, Any]] | None = None) -> FunctionModel:
    """Judges one text per call: finds the draft's entry unsupported and everything else
    supported. `seen` collects each call's text and item IDs."""

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = first_prompt(messages)
        text = data_block(prompt, "text")
        items = data_block(prompt, "items")
        if seen is not None:
            seen.append(text | {"items": [item["item_id"] for item in items]})
        verdict = make_verdict(claim="Signed two customers" if text["text"] == ENTRY else None)
        return text_response(JudgeOutput(unsupported=verdict.unsupported).model_dump_json())

    return FunctionModel(model)


def _tools(
    store: SqliteStore,
    outputs: Mapping[str, Extraction] = OUTPUTS,
    sensitivity: FunctionModel | None = None,
    consolidator: FunctionModel | None = None,
    writer: FunctionModel | None = None,
    conversation: FakeMailbox | None = None,
    judge: FunctionModel | None = None,
    splits: Mapping[str, list[str]] | None = None,
) -> Tools:
    return Tools(
        make_operations(store, FakeMailbox(INBOX), conversation),
        store,
        ExtractorAgent(extractor_model(outputs), CATEGORIES),
        DecomposerAgent(decomposer_model(splits or {})),
        SensitivityAgent(sensitivity or sensitivity_model(outputs)),
        ConsolidatorAgent(
            consolidator or FunctionModel(reply_with(CONSOLIDATOR_OUTPUT.model_dump_json())),
            CATEGORIES,
        ),
        WriterAgent(
            writer or FunctionModel(reply_with(DRAFT.model_dump_json())),
            CATEGORIES,
            {"customer_win": "Customer wins"},
            "Headline of the week",
            400,
        ),
        JudgeAgent(judge or judge_model()),
    )


async def _extracted(tools: Tools) -> str:
    """Start a newsletter and extract its screened emails, returning the newsletter ID."""
    started = await tools.start_newsletter()
    assert not isinstance(started, str)
    await tools.extract()
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
        "judge",
        "present_draft",
        "approve",
        "withdraw_approval",
        "abandon",
    }


async def test_tools_called_together_run_one_at_a_time_in_call_order(store: SqliteStore) -> None:
    model = responses(
        _call("start_newsletter"),
        ModelResponse(parts=[ToolCallPart("extract", {}), ToolCallPart("consolidate", {})]),
        text_response("Done."),
    )
    orchestrator = make_orchestrator(store, model, _tools(store).toolset())

    await orchestrator.handle(make_message(text="Please draft a newsletter."))

    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    # consolidate ran after extract had stored the records it reads.
    assert await store.get_items(newsletter.newsletter_id) == CONSOLIDATION


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


async def test_get_newsletter_before_any_newsletter_exists(store: SqliteStore) -> None:
    assert await _tools(store).get_newsletter() == "No newsletter exists yet."


async def test_get_newsletter_summarises_the_open_newsletter(store: SqliteStore) -> None:
    tools = _tools(store)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)
    await tools.extract()

    assert await tools.get_newsletter() == {
        "newsletter_id": started.newsletter_id,
        "state": "in_review",
        "latest_version": None,
        "approved_version": None,
        "send_time": None,
        "sent_at": None,
        "items": 0,
        # m01 is included but not yet consolidated.
        "items_up_to_date": False,
        "excluded_records": 2,
        "draft_changed": False,
    }


async def test_list_screened_emails_never_returns_subjects_or_bodies(store: SqliteStore) -> None:
    tools = _tools(store)
    await tools.start_newsletter()
    await tools.extract()

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
    # The pre-filter rejected m13, so it is never extracted.
    assert listed[3]["extract"] is None
    text = json.dumps(listed)
    assert "Signed Northwind Retail today" not in text
    assert "Nothing to report." not in text


async def test_tools_refuse_before_any_newsletter_exists(store: SqliteStore) -> None:
    tools = _tools(store)

    assert await tools.list_screened_emails() == "Refused: no newsletter exists yet"
    assert await tools.extract() == "Refused: no newsletter is open"
    assert await tools.consolidate() == "Refused: no newsletter is open"
    assert await tools.get_items() == "Refused: no newsletter exists yet"
    assert await tools.write("Write the first draft.") == "Refused: no newsletter is open"
    assert await tools.get_draft() == "Refused: no newsletter exists yet"
    assert await tools.present_draft(_run_for(make_message())) == "Refused: no newsletter is open"


async def test_extract_stores_each_record_with_its_exclusion(store: SqliteStore) -> None:
    tools = _tools(store)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    result = await tools.extract()

    assert not isinstance(result, str)
    assert (result.included, result.excluded, result.excluded_by_reason, result.failed) == (
        1,
        2,
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


async def test_extract_stores_each_record_with_its_facts_split_by_the_decomposer(
    store: SqliteStore,
) -> None:
    [fact] = OUTPUTS[INBOX[0].body].facts
    split = [fact, "Onboarding starts in October."]
    tools = _tools(store, splits={fact: split})
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    await tools.extract()

    records = {r.message_id: r for r in await store.list_extract_records(started.newsletter_id)}
    assert records["m01"].facts == split


def _line_by_line(email: ScreenedEmail, facts: Mapping[int, list[str]]) -> ExtractorAgent:
    """An extractor answering the email with the given facts under each line."""
    output = ExtractorOutput(
        category="customer_win",
        exclusion_reason=None,
        summary="News.",
        people=[],
        lines=[LineFacts(line=n, facts=facts.get(n, [])) for n in range(len(message_lines(email)))],
    )
    return ExtractorAgent(FunctionModel(reply_with(output.model_dump_json())), CATEGORIES)


async def test_extract_news_splits_each_line_with_facts_in_its_own_call() -> None:
    fact = "Signed Northwind Retail on 22 September, and Contoso on 1 October."
    split = ["Signed Northwind Retail on 22 September.", "Signed Contoso on 1 October."]
    email = make_screened_email(subject="Signed", body=f"Hi all,\n{fact}\nPriya")
    sent: list[str] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.append(first_prompt(messages))
        return text_response(DecomposerOutput(facts=split).model_dump_json())

    decomposer = DecomposerAgent(FunctionModel(model))
    output = await extract_news(_line_by_line(email, {2: [fact]}), decomposer, email)

    assert sent == [decomposer.message(DecomposerTask(line=fact, facts=[fact]))]
    assert [line.facts for line in output.lines] == [[], [], split, []]


async def test_a_gateway_error_fails_extract_news_once_every_other_call_has_finished() -> None:
    email = make_screened_email(subject="Signed", body="Line one.\nLine two.")
    finished: list[str] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "first" in str(messages[0]):
            raise gateway_error()
        await asyncio.sleep(0.05)
        finished.append("second")
        return text_response(DecomposerOutput(facts=["The second fact."]).model_dump_json())

    extractor = _line_by_line(email, {1: ["The first fact."], 2: ["The second fact."]})

    with pytest.raises(ModelHTTPError):
        await extract_news(extractor, DecomposerAgent(FunctionModel(model)), email)

    assert finished == ["second"]


async def test_extract_takes_only_emails_that_passed_and_have_no_record(
    store: SqliteStore,
) -> None:
    tools = _tools(store)
    await tools.start_newsletter()
    await tools.extract()

    # m13 was rejected by the pre-filter, and the others already have records.
    result = await tools.extract()

    assert not isinstance(result, str)
    assert (
        result.outcomes,
        result.included,
        result.excluded,
        result.excluded_by_reason,
        result.failed,
    ) == ([], 0, 0, {}, 0)


async def test_extract_reports_an_invalid_response_and_retries_it_when_called_again(
    store: SqliteStore,
) -> None:
    invalid = OUTPUTS | {INBOX[0].body: make_output(category="gossip")}
    tools = _tools(store, invalid)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    result = await tools.extract()

    assert not isinstance(result, str)
    assert result.failed == 1
    [failed] = [outcome for outcome in result.outcomes if outcome.error]
    assert failed.message_id == "m01"
    # The orchestrator can name the email to the reviewer without another tool call.
    assert (failed.sender_name, failed.received) == (INBOX[0].sender_name, INBOX[0].received)
    assert failed.error is not None and "gossip is not a configured category" in failed.error
    records = await store.list_extract_records(started.newsletter_id)
    assert {r.message_id for r in records} == {"m02", "m03"}
    # The failed email still has no record, so the next call tries it again.
    tools = _tools(store)
    retried = await tools.extract()
    assert not isinstance(retried, str)
    assert [o.message_id for o in retried.outcomes] == ["m01"]


async def test_extract_fails_an_email_when_the_sensitivity_agent_fails_and_retries_it(
    store: SqliteStore,
) -> None:
    invalid = FunctionModel(reply_with('{"sensitivity": "none"}'))
    tools = _tools(store, sensitivity=invalid)
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    result = await tools.extract()

    assert not isinstance(result, str)
    assert (result.failed, len(result.outcomes)) == (3, 3)
    assert all(outcome.error for outcome in result.outcomes)
    assert await store.list_extract_records(started.newsletter_id) == []
    # No email has a record, so the next call tries every one again.
    retried = await _tools(store).extract()
    assert not isinstance(retried, str)
    assert retried.failed == 0
    assert {o.message_id for o in retried.outcomes} == {"m01", "m02", "m03"}


async def test_a_gateway_error_fails_extract_once_every_other_email_has_finished(
    store: SqliteStore,
) -> None:
    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if INBOX[0].body in str(messages[0]):
            raise gateway_error()
        await asyncio.sleep(0.05)
        return text_response('{"sensitivity": []}')

    tools = _tools(store, sensitivity=FunctionModel(model))
    started = await tools.start_newsletter()
    assert not isinstance(started, str)

    with pytest.raises(ModelHTTPError):
        await tools.extract()

    # The other emails' records were saved before the tool failed, so none is saved after the
    # run has failed; the failed email has none, so the next call retries it.
    records = await store.list_extract_records(started.newsletter_id)
    assert {r.message_id for r in records} == {"m02", "m03"}


async def test_corpus_starts_and_extracts_as_the_corpus_expects(store: SqliteStore) -> None:
    messages = corpus_messages()
    inbox = [corpus_email(message, n) for n, message in enumerate(messages)]
    outputs = {
        m["body"]: Extraction.model_validate(m["extract"]) for m in messages if "extract" in m
    }
    categories = {
        "customer_win": "A new customer has signed.",
        "delivery_highlight": "A project has been delivered.",
        "team_news": "Someone has joined.",
        "shout_out": "A colleague is thanked.",
        "company_notices": "Information staff should know.",
    }
    consolidator = corpus_consolidator_model()
    tools = Tools(
        make_operations(store, FakeMailbox(inbox)),
        store,
        ExtractorAgent(extractor_model(outputs), categories),
        DecomposerAgent(decomposer_model({})),
        SensitivityAgent(sensitivity_model(outputs)),
        ConsolidatorAgent(consolidator, categories),
        WriterAgent(
            FunctionModel(reply_with(DRAFT.model_dump_json())),
            categories,
            {"customer_win": "Customer wins"},
            "Headline of the week",
            400,
        ),
        JudgeAgent(judge_model()),
    )
    model = responses(
        _call("get_newsletter"),
        _call("start_newsletter"),
        _call("extract"),
        _call("consolidate"),
        text_response("Done."),
    )
    orchestrator = make_orchestrator(store, model, tools.toolset())

    reply = await orchestrator.handle(make_message(text="Please draft a newsletter."))
    assert reply.text == "Done."

    newsletter = await store.get_open_newsletter()
    assert newsletter is not None
    assert await stored_outcomes(store, newsletter.newsletter_id) == CORPUS_OUTCOMES
    records = await store.list_extract_records(newsletter.newsletter_id)
    excluded_ids = sorted(r.excluded_id for r in records if r.excluded_id)
    excluded = sum(1 for outcome in CORPUS_OUTCOMES.values() if outcome.startswith("excluded"))
    assert excluded_ids == sorted(f"excluded-{n}" for n in range(1, excluded + 1))
    items = await store.get_items(newsletter.newsletter_id)
    assert items is not None
    assert [item.source_message_ids for item in items.items] == CORPUS_ITEM_SOURCES


async def test_consolidate_stores_the_items_and_headline(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _extracted(tools)

    result = await tools.consolidate()

    assert result == {
        "headline": "A new retail customer",
        "item_count": 1,
        "items": [{"item_id": "item-1", "category": "customer_win", "source_message_ids": ["m01"]}],
    }
    assert await store.get_items(newsletter_id) == CONSOLIDATION
    newsletter = await tools.get_newsletter()
    assert isinstance(newsletter, dict)
    assert (newsletter["items"], newsletter["items_up_to_date"]) == (1, True)


async def test_consolidate_refuses_without_an_included_record(store: SqliteStore) -> None:
    excluded = {body: make_output(category=None) for body in OUTPUTS}
    tools = _tools(store, excluded)
    newsletter_id = await _extracted(tools)

    result = await tools.consolidate()

    assert result == "Refused: the newsletter has no included extract records"
    assert await store.get_items(newsletter_id) is None


async def test_consolidate_reports_an_invalid_response_and_stores_nothing(
    store: SqliteStore,
) -> None:
    invalid = make_consolidator_output(
        make_consolidator_item("record-1", "record-9")
    ).model_dump_json()
    consolidator = FunctionModel(responses(text_response(invalid), text_response(invalid)))
    tools = _tools(store, consolidator=consolidator)
    newsletter_id = await _extracted(tools)

    result = await tools.consolidate()

    assert result == (
        "Failed: consolidator gave no valid response: source record-9 is not an input record"
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
    await tools.consolidate()

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
    await tools.consolidate()
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
    assert data_block(prompt, "instruction") == "Write the first draft."
    assert "<working_draft>" not in prompt


async def test_write_revises_the_working_draft_with_all_feedback(store: SqliteStore) -> None:
    prompts: list[str] = []
    tools = _tools(store, writer=_writer_prompts(prompts))
    newsletter_id = await _consolidated(tools)
    await store.record_feedback(newsletter_id, make_message("r01", text="Shorter intro, please."))
    await tools.write("Write the first draft.")

    await tools.present_draft(_run_for(make_message()))

    revised = await tools.write("Shorten the intro.")

    # The stand-in returns the same draft again, so it has not changed since v1.
    assert isinstance(revised, dict)
    assert revised["draft_changed"] is False
    assert data_block(prompts[-1], "working_draft") == DRAFT.content.model_dump(mode="json")
    assert data_block(prompts[-1], "feedback") == [
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
    await tools.present_draft(_run_for(make_message()))

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
    await tools.present_draft(_run_for(make_message()))

    assert await tools.show_draft() == ShowResult(version=None)
    assert await tools.show_draft(1) == ShowResult(version=1)
    assert await tools.show_draft(2) == "Refused: v2 has not been presented"


async def test_present_draft_emails_the_next_version(store: SqliteStore) -> None:
    conversation = FakeMailbox()
    tools = _tools(store, conversation=conversation)
    await _consolidated(tools)
    assert (
        await tools.present_draft(_run_for(make_message())) == "Refused: there is no working draft"
    )
    await tools.write("Write the first draft.")

    result = await tools.present_draft(_run_for(make_message()))

    assert not isinstance(result, str)
    assert result.version == 1
    [email] = conversation.sent
    assert email.to == [REVIEWERS]


async def test_present_draft_in_the_email_channel_sends_no_email(store: SqliteStore) -> None:
    conversation = FakeMailbox()
    tools = _tools(store, conversation=conversation)
    await _consolidated(tools)
    await tools.write("Write the first draft.")

    result = await tools.present_draft(_run_for(make_message("c01", channel="email")))

    assert not isinstance(result, str)
    assert result.version == 1
    # The email channel's reply carries the version instead.
    assert (conversation.sent, conversation.replies) == ([], [])


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
    await tools.present_draft(_run_for(make_message()))
    assert await draft_changed() is False
    shorter = DRAFT.content.model_copy(update={"intro": "A shorter intro."})
    await store.save_draft(newsletter_id, DRAFT.model_copy(update={"content": shorter}))
    assert await draft_changed() is True


async def test_judge_runs_the_code_checks_and_returns_every_failure(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _consolidated(tools)
    dashed = DRAFT.content.model_copy(update={"intro": "A strong week \N{EN DASH} again."})
    await store.save_draft(newsletter_id, DRAFT.model_copy(update={"content": dashed}))

    result = await tools.judge()

    assert isinstance(result, dict)
    assert (result["failure_count"], result["failures"]) == (
        1,
        [{"check": "dashes", "target": "intro", "detail": "em or en dash"}],
    )


async def test_judge_works_only_on_the_open_newsletter(store: SqliteStore) -> None:
    await store.save_newsletter(abandon(make_newsletter(), REVIEWER, OPENED))

    assert await _tools(store).judge() == "Refused: no newsletter is open"


async def test_a_gateway_error_fails_judge_once_every_other_call_has_finished(
    store: SqliteStore,
) -> None:
    finished: list[str] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if '"part": "intro"' in first_prompt(messages):
            raise gateway_error()
        await asyncio.sleep(0.05)
        finished.append("entry")
        return text_response(JudgeOutput(unsupported=[]).model_dump_json())

    tools = _tools(store, judge=FunctionModel(model))
    await _consolidated(tools)
    await tools.write("Write the first draft.")

    with pytest.raises(ModelHTTPError):
        await tools.judge()

    # The entry's call finished before the tool failed, so none runs on after it.
    assert finished == ["entry"]


async def test_judge_stores_its_verdicts_and_returns_what_is_unsupported(
    store: SqliteStore,
) -> None:
    seen: list[dict[str, Any]] = []
    tools = _tools(store, judge=judge_model(seen))
    newsletter_id = await _consolidated(tools)
    assert await tools.judge() == "Refused: there is no working draft"
    await tools.write("Write the first draft.")

    result = await tools.judge()

    assert result == {
        "failure_count": 0,
        "failures": [],
        "judged": 2,
        "unsupported": [
            {
                "target": "item-1",
                "unsupported": [
                    {"text": "Signed two customers", "reason": "Nothing in the facts states it."}
                ],
            }
        ],
    }
    assert await store.get_verdicts(newsletter_id) == JUDGED
    # One call per text: the intro with every item, each entry with only its own item.
    assert seen == [
        {"part": "intro", "text": DRAFT.content.intro, "items": ["item-1"]},
        {"part": "entry", "text": ENTRY, "items": ["item-1"]},
    ]


async def test_judge_gives_an_entry_only_its_own_item(store: SqliteStore) -> None:
    seen: list[dict[str, Any]] = []
    consolidator = FunctionModel(
        reply_with(
            make_consolidator_output(
                make_consolidator_item("record-1"), make_consolidator_item("record-2")
            ).model_dump_json()
        )
    )
    outputs = OUTPUTS | {INBOX[1].body: make_output(facts=["Contoso renewed its contract."])}
    tools = _tools(
        store,
        outputs,
        consolidator=consolidator,
        judge=judge_model(seen),
    )
    newsletter_id = await _extracted(tools)
    await tools.consolidate()
    await store.save_draft(newsletter_id, DRAFT)

    await tools.judge()

    assert [call["items"] for call in seen] == [["item-1", "item-2"], ["item-1"]]


async def test_judge_reports_an_invalid_response_and_stores_nothing(store: SqliteStore) -> None:
    invalid = '{"unsupported": "Made up"}'
    tools = _tools(store, judge=FunctionModel(reply_with(invalid)))
    newsletter_id = await _consolidated(tools)
    await tools.write("Write the first draft.")

    assert await tools.judge() == (
        "Failed: judge gave no valid response: the response did not match the output type"
    )
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

    result = await tools.restore(_run_for(make_message()), excluded_id, "Customer wins")

    assert result == RestoreResult(excluded_id=excluded_id, message_id="m02")
    records = {r.message_id: r for r in await store.list_extract_records(newsletter_id)}
    assert (records["m02"].exclusion, records["m02"].restored_by) == (None, REVIEWER)
    assert records["m02"].category == "customer_win"
    # The restored record is not in the items until consolidate runs again.
    summary = await tools.get_newsletter()
    assert isinstance(summary, dict)
    assert summary["items_up_to_date"] is False


async def test_restore_refuses_an_id_that_names_no_excluded_record(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _consolidated(tools)
    excluded_id = await _excluded_id(store, newsletter_id, "m02")
    await tools.restore(_run_for(make_message()), excluded_id, "Customer wins")

    assert await tools.restore(_run_for(make_message()), excluded_id, "Customer wins") == (
        f"Refused: {excluded_id} names no excluded record in the open newsletter"
    )


async def test_restore_asks_for_a_section_for_a_record_with_none(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _consolidated(tools)
    excluded_id = await _excluded_id(store, newsletter_id, "m02")

    assert await tools.restore(_run_for(make_message()), excluded_id) == (
        f"Refused: {excluded_id} fits no section, so the reviewer must name one of: "
        "Customer wins, Shout-outs"
    )


async def test_restore_refuses_without_an_open_newsletter(store: SqliteStore) -> None:
    assert await _tools(store).restore(_run_for(make_message()), "excluded-1") == (
        "Refused: no newsletter is open"
    )


def _call(tool: str, args: dict[str, object] | None = None) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool, args or {})])


async def _presented(tools: Tools) -> str:
    """Start a newsletter and present version 1, returning the newsletter ID."""
    newsletter_id = await _consolidated(tools)
    await tools.write("Write the first draft.")
    await tools.present_draft(_run_for(make_message()))
    return newsletter_id


async def test_approve_takes_the_reviewer_and_message_from_the_run(store: SqliteStore) -> None:
    tools = _tools(store)
    newsletter_id = await _presented(tools)

    result = await tools.approve(_run_for(make_message(text="Approve v1\nThanks!")), 1)

    assert isinstance(result, ApproveResult)
    assert result.version == 1
    newsletter = await store.get_latest_newsletter()
    assert newsletter is not None
    assert (newsletter.newsletter_id, newsletter.state, newsletter.approver) == (
        newsletter_id,
        "approved",
        REVIEWER,
    )


async def test_approve_refuses_without_the_reviewers_own_words(store: SqliteStore) -> None:
    tools = _tools(store)
    await _presented(tools)

    result = await tools.approve(_run_for(make_message(text="Looks good to me")), 1)

    assert result == "Refused: the reviewer's message does not start with approve v1"


async def test_withdraw_approval_returns_the_newsletter_to_review(store: SqliteStore) -> None:
    conversation = FakeMailbox()
    tools = _tools(store, conversation=conversation)
    await _presented(tools)
    await tools.approve(_run_for(make_message(text="approve v1")), 1)

    assert await tools.withdraw_approval(_run_for(make_message())) == NoticeResult(notice_sent=True)
    assert await tools.withdraw_approval(_run_for(make_message())) == (
        "Refused: the newsletter is not approved"
    )
    summary = await tools.get_newsletter()
    assert isinstance(summary, dict)
    assert (summary["state"], summary["approved_version"]) == ("in_review", None)


async def test_abandon_closes_the_newsletter(store: SqliteStore) -> None:
    tools = _tools(store)
    await _presented(tools)

    assert await tools.abandon(_run_for(make_message())) == NoticeResult(notice_sent=True)
    assert await tools.abandon(_run_for(make_message())) == "Refused: no newsletter is open"


async def test_read_tools_still_work_once_the_newsletter_is_closed(store: SqliteStore) -> None:
    tools = _tools(store)
    await _presented(tools)
    await tools.abandon(_run_for(make_message()))

    summary = await tools.get_newsletter()
    assert isinstance(summary, dict) and summary["state"] == "abandoned"
    assert isinstance(await tools.list_screened_emails(), list)
    assert isinstance(await tools.get_items(), dict)
    assert isinstance(await tools.get_draft(1), dict)
    assert isinstance(await tools.get_draft(), dict)
    assert await tools.show_draft(1) == ShowResult(version=1)


async def test_action_tools_refuse_once_the_newsletter_is_closed(store: SqliteStore) -> None:
    tools = _tools(store)
    await _presented(tools)
    await tools.abandon(_run_for(make_message()))
    refused = "Refused: no newsletter is open"

    assert await tools.extract() == refused
    assert await tools.consolidate() == refused
    assert await tools.write("Shorten the intro.") == refused
    assert await tools.judge() == refused
    assert await tools.present_draft(_run_for(make_message())) == refused
    assert await tools.approve(_run_for(make_message(text="approve v1")), 1) == refused
    assert await tools.withdraw_approval(_run_for(make_message())) == refused
