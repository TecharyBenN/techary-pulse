import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import httpx2
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pulse.agents.extractor.agent import ExtractorAgent
from pulse.agents.writer.agent import WriterTask
from pulse.config import load_config
from pulse.entities.content import (
    Content,
    Entry,
    JudgeOutput,
    Section,
    UnsupportedText,
    WriterOutput,
)
from pulse.entities.extracts import (
    ConsolidatorItem,
    ConsolidatorOutput,
    ExtractorOutput,
    SensitivityOutput,
    with_sources,
)
from tests.emails import (
    corpus_email,
    corpus_expectation,
    corpus_messages,
    make_item,
    make_output,
    make_screened_email,
    share,
)
from tests.emails import screen_email as screened
from tests.evaluation import (
    Answer,
    ConsolidatorExpectation,
    Exchange,
    ExtractorExpectation,
    Input,
    JudgeExpectation,
    OrchestratorScenarios,
    Recorder,
    Results,
    Scenario,
    ScenarioExpectation,
    SensitivityExpectation,
    Turn,
    chosen_inputs,
    consolidator_expectations,
    consolidator_inputs,
    consolidator_problems,
    corpus_inputs,
    dense_notice_expectations,
    dense_notice_inputs,
    evaluate,
    expectations,
    extractor_problems,
    judge_inputs,
    judge_problems,
    orchestrator_expectations,
    orchestrator_inputs,
    orchestrator_problems,
    save_results,
    sensitivity_problems,
    token_totals,
    writer_expectations,
    writer_inputs,
    writer_problems,
)

pytestmark = pytest.mark.anyio

CATEGORIES = {"customer_win": "A new customer has signed."}
EXPECTED = ExtractorExpectation(
    category="customer_win",
    has_facts=True,
    people=["Priya Shah", "Tom Evans"],
    mention=("Northwind Retail", "three-year", "22 September"),
    omit=("CV",),
)
RIGHT = make_output(
    facts=[
        "Priya Shah and Tom Evans signed Northwind Retail to a three-year contract on 22 September"
    ]
)
API_KEY = "stand-in-key"


def _output(**changes: object) -> ExtractorOutput:
    return share(RIGHT.model_copy(update=changes), ExtractorOutput)


def test_a_right_answer_has_no_problems() -> None:
    assert extractor_problems(_output(), EXPECTED) == []


def test_text_is_compared_ignoring_case_punctuation_and_spacing() -> None:
    facts = ["priya shah and TOM EVANS signed Northwind  Retail, three year deal, 22 September."]

    assert (
        extractor_problems(_output(facts=facts, people=["priya shah", "Tom Evans"]), EXPECTED) == []
    )


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"category": "shout_out"}, "category shout_out instead of customer_win"),
        (
            {"people": ["Priya Shah"]},
            "people ['Priya Shah'] instead of ['Priya Shah', 'Tom Evans']",
        ),
        (
            {"facts": ["Priya Shah and Tom Evans signed Northwind Retail on 22 September"]},
            "the facts do not mention three-year",
        ),
        (
            {"facts": [*RIGHT.facts, "Her CV is attached"]},
            "the facts mention CV",
        ),
    ],
)
def test_each_departure_is_a_problem(changes: dict[str, object], problem: str) -> None:
    assert extractor_problems(_output(**changes), EXPECTED) == [problem]


def test_no_facts_for_an_email_with_news_are_a_problem_with_each_missing_mention() -> None:
    assert extractor_problems(_output(facts=[]), EXPECTED) == [
        "no facts for an email with news",
        "the facts do not mention Northwind Retail",
        "the facts do not mention three-year",
        "the facts do not mention 22 September",
    ]


def test_facts_for_an_email_with_no_news_are_a_problem() -> None:
    expected = ExtractorExpectation(category=None, has_facts=False, people=[])
    output = _output(category=None, exclusion_reason="A test.", people=[])

    assert extractor_problems(output, expected) == ["facts given for an email with no news"]


OPTIONAL = EXPECTED.model_copy(update={"has_facts": None})


def test_optional_facts_are_checked_when_given() -> None:
    assert extractor_problems(_output(), OPTIONAL) == []
    assert extractor_problems(_output(people=["Priya Shah"]), OPTIONAL) == [
        "people ['Priya Shah'] instead of ['Priya Shah', 'Tom Evans']"
    ]


def test_optional_facts_may_be_left_out_and_then_name_no_one() -> None:
    assert extractor_problems(_output(facts=[], people=[]), OPTIONAL) == []
    assert extractor_problems(_output(facts=[]), OPTIONAL) == [
        "people ['Priya Shah', 'Tom Evans'] instead of []"
    ]


def test_a_mention_matches_whole_words_only() -> None:
    expected = EXPECTED.model_copy(update={"mention": ("Ava",)})

    problems = extractor_problems(
        _output(facts=["Priya Shah and Tom Evans signed Avanade"]), expected
    )

    assert problems == ["the facts do not mention Ava"]


def test_every_corpus_email_that_passes_the_pre_filter_is_an_input_with_an_expectation() -> None:
    messages = corpus_messages()
    passing = {
        m["id"] for n, m in enumerate(messages) if screened(corpus_email(m, n)).rejection is None
    }

    inputs = corpus_inputs()

    assert sorted(i.input_id for i in inputs) == sorted(passing)
    assert set(expectations("extractor", ExtractorExpectation)) == passing
    assert set(expectations("sensitivity", SensitivityExpectation)) == passing
    # Only emails kept out of the dev inbox are built locally; the rest are as Graph returned them.
    local = {m["id"] for m in messages if not m.get("seed", True)}
    assert {i.input_id for i in inputs if i.form == "local"} == local & passing


def test_words_that_must_be_apart_in_one_fact_are_a_problem() -> None:
    expected = EXPECTED.model_copy(update={"mention": (), "apart": (("1 March", "1 June"),)})
    packed = _output(facts=["Litware stops selling from 1 March, except to schools (1 June)."])
    split = _output(facts=["Litware stops selling from 1 March.", "For schools, from 1 June."])

    assert extractor_problems(split, expected) == []
    assert extractor_problems(packed, expected) == ["1 March and 1 June are in one fact"]


def test_every_dense_notice_is_an_input_with_an_expectation() -> None:
    inputs = dense_notice_inputs()
    expected = dense_notice_expectations()

    assert [i.input_id for i in inputs] == list(expected)
    assert all(i.task.rejection is None and i.form == "synthetic" for i in inputs)
    assert all(e.apart and e.mention for e in expected.values())


FLAGGED = SensitivityExpectation(flags={"named_person": False, "personal_information": False})


def _flags(*flags: tuple[str, bool]) -> SensitivityOutput:
    return SensitivityOutput.model_validate(
        {"sensitivity": [{"kind": k, "withheld": w, "evidence": "e"} for k, w in flags]}
    )


def test_a_right_set_of_flags_has_no_problems() -> None:
    output = _flags(("named_person", False), ("personal_information", False))

    assert sensitivity_problems(output, FLAGGED) == []


def test_several_flags_of_one_kind_count_once() -> None:
    output = _flags(
        ("named_person", False), ("named_person", False), ("personal_information", False)
    )

    assert sensitivity_problems(output, FLAGGED) == []


@pytest.mark.parametrize(
    ("flags", "problems"),
    [
        ((("named_person", False),), ["no personal_information flag"]),
        (
            (("named_person", False), ("personal_information", False), ("financial", False)),
            ["an extra financial flag"],
        ),
        (
            (("named_person", False), ("personal_information", True)),
            [
                "withheld instead of included",
                "personal_information withheld instead of not withheld",
            ],
        ),
    ],
)
def test_each_flag_departure_is_a_problem(
    flags: tuple[tuple[str, bool], ...], problems: list[str]
) -> None:
    assert sensitivity_problems(_flags(*flags), FLAGGED) == problems


def test_a_missed_withheld_flag_is_an_included_email() -> None:
    expected = SensitivityExpectation(flags={"personal_information": True, "confidential": True})
    output = _flags(("personal_information", False))

    assert sensitivity_problems(output, expected) == [
        "included instead of withheld",
        "personal_information not withheld instead of withheld",
        "no confidential flag",
    ]


def test_an_optional_flag_may_be_raised_or_not() -> None:
    expected = SensitivityExpectation(flags={"financial": True, "personal_information": None})

    assert sensitivity_problems(_flags(("financial", True)), expected) == []
    raised = _flags(("financial", True), ("personal_information", True))
    assert sensitivity_problems(raised, expected) == []


def test_an_optional_flag_alone_does_not_withhold_the_email() -> None:
    expected = SensitivityExpectation(flags={"personal_information": None})

    assert sensitivity_problems(_flags(("personal_information", True)), expected) == [
        "withheld instead of included"
    ]


MERGED = ConsolidatorExpectation.model_validate(
    {
        "items": [
            {"sources": ["m01", "m02"], "category": "customer_win", "mention": ["Northwind"]},
            {"sources": ["m03"], "category": "delivery_highlight", "mention": ["Contoso"]},
        ]
    }
)
SOURCES = {"record-1": "m01", "record-2": "m02", "record-3": "m03"}


def _items(*items: tuple[list[str], str, list[str]]) -> ConsolidatorOutput:
    return ConsolidatorOutput(
        headline="A new customer and a launch",
        items=[
            ConsolidatorItem(category=category, facts=facts, source_record_ids=records)
            for records, category, facts in items
        ],
    )


def test_items_merged_as_expected_have_no_problems() -> None:
    output = _items(
        (["record-2", "record-1"], "customer_win", ["Northwind Retail signed."]),
        (["record-3"], "delivery_highlight", ["The Contoso migration went live."]),
    )

    assert consolidator_problems(output, SOURCES, MERGED) == []


def test_records_split_from_their_item_are_a_problem() -> None:
    output = _items(
        (["record-1"], "customer_win", ["Northwind Retail signed."]),
        (["record-2"], "customer_win", ["Northwind Retail signed."]),
        (["record-3"], "delivery_highlight", ["The Contoso migration went live."]),
    )

    assert consolidator_problems(output, SOURCES, MERGED) == [
        "no item merging m01 + m02",
        "an unexpected item merging m01",
        "an unexpected item merging m02",
    ]


def test_a_merged_item_with_the_wrong_category_or_missing_facts_is_a_problem() -> None:
    output = _items(
        (["record-1", "record-2"], "shout_out", ["A deal was signed."]),
        (["record-3"], "delivery_highlight", ["The Contoso migration went live."]),
    )

    assert consolidator_problems(output, SOURCES, MERGED) == [
        "m01 + m02: category shout_out instead of customer_win",
        "m01 + m02: the facts do not mention Northwind",
    ]


def test_each_consolidator_input_has_expected_items_holding_every_record_once() -> None:
    inputs = {item.input_id: item for item in consolidator_inputs()}
    expected = consolidator_expectations()

    assert set(inputs) == set(expected) == {"included", "restored"}
    for input_id, item in inputs.items():
        records = [record.message_id for record in item.task.records]
        sources = [source for i in expected[input_id].items for source in i.sources]
        assert sorted(sources) == sorted(records)
    # Restored records are given in corpus order among the others, as the store lists them.
    restored = [record.message_id for record in inputs["restored"].task.records]
    assert restored == sorted(restored)
    assert set(restored) - {r.message_id for r in inputs["included"].task.records} == {
        "m22",
        "m23",
    }
    assert all(record.exclusion is None for record in inputs["restored"].task.records)


WRITER_EMAILS = [
    make_screened_email("m01"),
    make_screened_email(
        "m05", sender_name="Dan Wood", body="Huge thanks to Sam Patel for covering the desk."
    ),
]
WRITER_TASK = WriterTask(
    headline="A new customer",
    items=with_sources(
        [
            make_item(source_message_ids=["m01"]),
            make_item(
                "item-2",
                category="shout_out",
                facts=["Sam Patel covered the service desk"],
                source_message_ids=["m05"],
                people=["Sam Patel"],
            ),
        ],
        WRITER_EMAILS,
    ),
    excluded=[],
    feedback=[],
    instruction="Write the first draft.",
    draft=None,
)
WRITTEN = ConsolidatorExpectation.model_validate(
    {
        "items": [
            {"sources": ["m01"], "category": "customer_win", "mention": ["Northwind Retail"]},
            {"sources": ["m05"], "category": "shout_out", "mention": ["Sam Patel", "service desk"]},
        ]
    }
)
NORTHWIND = Entry(
    item_id="item-1",
    text="Priya Shah and Tom Evans signed Northwind Retail on 22 September.",
    people=["Priya Shah", "Tom Evans"],
)
THANKS = Entry(
    item_id="item-2", text="Thank you to Sam Patel for covering the service desk.", people=[]
)


def _draft(customer_win: list[Entry], shout_out: list[Entry]) -> WriterOutput:
    sections = [
        Section(category="customer_win", title="Customer wins", entries=customer_win),
        Section(category="shout_out", title="Shout-outs", entries=shout_out),
    ]
    entries = [entry.item_id for section in sections for entry in section.entries]
    content = Content(
        headline_title="Headline of the week",
        headline="A new customer",
        intro="A good week.",
        sections=sections,
        item_ids=entries,
    )
    return WriterOutput(content=content, changes=[], not_applied=[])


def _writer_problems(output: WriterOutput) -> list[str]:
    return writer_problems(
        output, WRITER_TASK, WRITTEN, WRITER_EMAILS, ["customer_win", "shout_out"], 400
    )


def test_a_draft_with_every_item_in_its_section_has_no_problems() -> None:
    assert _writer_problems(_draft([NORTHWIND], [THANKS])) == []


def test_an_entry_in_another_section_or_missing_a_fact_is_a_problem() -> None:
    thanks = THANKS.model_copy(update={"text": "Thank you to Sam Patel."})

    assert _writer_problems(_draft([NORTHWIND, thanks], [])) == [
        "m05: in customer_win instead of shout_out",
        "m05: the entry does not mention service desk",
    ]


def test_an_entry_naming_a_sender_who_only_shared_it_is_a_problem() -> None:
    thanks = THANKS.model_copy(
        update={"text": "Dan Wood thanks Sam Patel for covering the service desk."}
    )

    assert _writer_problems(_draft([NORTHWIND], [thanks])) == [
        "m05: the entry names Dan Wood, who only shared it"
    ]


def test_pulse_draft_check_failures_and_dropped_items_are_problems() -> None:
    dashed = NORTHWIND.model_copy(
        update={"text": "Priya Shah and Tom Evans signed Northwind Retail \N{EM DASH} great news."}
    )

    # The items check only compares entries with the draft's own item IDs, so a dropped item is
    # reported by the expected answer.
    assert _writer_problems(_draft([dashed], [])) == [
        "dashes check failed at item-1: em or en dash",
        "no entry for m05",
    ]


def test_an_entry_over_the_sentence_limit_is_not_a_problem() -> None:
    # The orchestrator's fix loop shortens a long entry, so a first draft's length is no defect.
    long = NORTHWIND.model_copy(
        update={"text": "Priya Shah and Tom Evans signed Northwind Retail. " * 6}
    )

    assert _writer_problems(_draft([long], [THANKS])) == []


def test_the_writer_input_holds_every_expected_item_once() -> None:
    item = writer_inputs()[0]
    sources = sorted(sorted(i.source_message_ids) for i in item.task.items)

    expected = consolidator_expectations()["included"]

    assert sources == sorted(sorted(i.sources) for i in expected.items)
    assert all(i.sender_names and i.received for i in item.task.items)
    assert item.task.excluded and all(r.exclusion for r in item.task.excluded)
    assert [m.text for m in item.task.feedback] == ["Please draft a newsletter."]
    assert item.task.draft is None


def test_every_writer_input_has_an_expectation() -> None:
    assert [i.input_id for i in writer_inputs()] == list(writer_expectations())


def _completion(content: str) -> dict[str, object]:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "stand-in-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }


def _gateway(recorder: Recorder, invalid: Mapping[str, int] | None = None) -> ExtractorAgent:
    """An extractor reaching a stand-in gateway through the recording client. It answers every
    email rightly, except that an email whose body `invalid` names first gets that many answers
    with an unknown category."""
    remaining = dict(invalid or {})

    def handler(request: httpx2.Request) -> httpx2.Response:
        prompt = json.dumps(json.loads(request.content)["messages"])
        output = _output()
        for body, count in remaining.items():
            if body in prompt and count:
                remaining[body] = count - 1
                output = _output(category="gossip")
        return httpx2.Response(200, json=_completion(output.model_dump_json()))

    client = recorder.client(transport=httpx2.MockTransport(handler))
    provider = OpenAIProvider(
        base_url="http://gateway.test/v1", api_key=API_KEY, http_client=client
    )
    return ExtractorAgent(OpenAIChatModel("stand-in-model", provider=provider), CATEGORIES)


INPUTS = [
    Input("m01", "Signed Northwind Retail today", "graph", make_screened_email("m01")),
    Input(
        "m02",
        "Contoso",
        "local",
        make_screened_email("m02", body="Tom Evans closed Contoso today."),
    ),
]


async def test_evaluate_scores_every_sample_with_the_exchanges_that_produced_it() -> None:
    recorder = Recorder()
    agent = _gateway(recorder)

    answers = await evaluate(
        agent.answer, lambda _, output: extractor_problems(output, EXPECTED), INPUTS, 2, recorder
    )

    assert [(a.input_id, a.form, a.sample) for a in answers] == [
        ("m01", "graph", 1),
        ("m02", "local", 1),
        ("m01", "graph", 2),
        ("m02", "local", 2),
    ]
    assert all(a.problems == [] and a.error is None for a in answers)
    assert all(a.output == _output().model_dump(mode="json") for a in answers)
    for answer in answers:
        [exchange] = answer.exchanges
        assert (exchange.status, exchange.model, exchange.finish_reason) == (
            200,
            "stand-in-model",
            "stop",
        )
        assert exchange.usage == {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
        }
    # The same email sends the same request in every sample, and each email its own.
    first = {
        c: {a.exchanges[0].request_sha256 for a in answers if a.input_id == c}
        for c in ("m01", "m02")
    }
    assert all(len(hashes) == 1 for hashes in first.values())
    assert first["m01"] != first["m02"]
    # The body sent is kept once by its hash, so the exact request can be read back.
    [m02_hash] = first["m02"]
    assert "Tom Evans closed Contoso today." in json.dumps(recorder.requests[m02_hash])
    # Headers are never recorded, so neither is the gateway key.
    recorded = json.dumps(recorder.requests) + json.dumps(
        [a.model_dump(mode="json") for a in answers]
    )
    assert API_KEY not in recorded


async def test_evaluate_keeps_the_retry_with_the_answer_it_belongs_to() -> None:
    recorder = Recorder()
    agent = _gateway(recorder, {"Tom Evans closed Contoso today.": 1})

    answers = await evaluate(
        agent.answer, lambda _, output: extractor_problems(output, EXPECTED), INPUTS, 1, recorder
    )

    exchanges = {a.input_id: len(a.exchanges) for a in answers}
    assert exchanges == {"m01": 1, "m02": 2}
    assert all(a.problems == [] for a in answers)


async def test_a_wrong_answer_is_recorded_with_its_problems() -> None:
    recorder = Recorder()
    expected = EXPECTED.model_copy(update={"category": None})

    answers = await evaluate(
        _gateway(recorder).answer,
        lambda _, output: extractor_problems(output, expected),
        INPUTS[:1],
        1,
        recorder,
    )

    [answer] = answers
    assert answer.problems == ["category customer_win instead of None"]
    assert answer.output is not None


async def test_no_valid_answer_is_recorded_as_a_problem_with_both_exchanges() -> None:
    recorder = Recorder()
    agent = _gateway(recorder, {"Tom Evans closed Contoso today.": 2})

    answers = await evaluate(
        agent.answer,
        lambda _, output: extractor_problems(output, EXPECTED),
        INPUTS[1:],
        1,
        recorder,
    )

    [answer] = answers
    assert answer.output is None
    assert answer.error is not None and "gossip is not a configured category" in answer.error
    assert answer.problems == [f"no valid response: {answer.error}"]
    assert len(answer.exchanges) == 2


def test_results_are_saved_privately(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.requests["abc"] = {"messages": [{"role": "user", "content": "Hello"}]}
    results = Results.of(
        agent="extractor",
        model="stand-in-model",
        base_url="http://gateway.test/v1",
        samples=1,
        started=make_screened_email().received,
        finished=make_screened_email().received,
        answers=[],
        recorder=recorder,
    )

    path = save_results(results, tmp_path / "evaluations")

    assert path == tmp_path / "evaluations" / "extractor" / "20260922T153000Z.json"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["requests"] == {"abc": {"messages": [{"role": "user", "content": "Hello"}]}}
    assert len(saved["commit"]) == 40


def test_token_totals_add_input_and_output_tokens_across_every_exchange() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    exchange = Exchange(
        request_sha256="abc",
        status=200,
        model="m",
        finish_reason="stop",
        usage=usage,
        content="{}",
    )
    failed = exchange.model_copy(update={"status": 502, "usage": None})
    answer = Answer(
        input_id="m01",
        form="graph",
        sample=1,
        output=None,
        error=None,
        problems=[],
        exchanges=[exchange, exchange, failed],
    )

    assert token_totals([answer]) == (200, 20)


def _found(*texts: str) -> JudgeOutput:
    return JudgeOutput(unsupported=[UnsupportedText(text=t, reason="Not stated.") for t in texts])


def test_a_supported_text_found_unsupported_is_a_problem() -> None:
    expected = JudgeExpectation(supported=True)

    assert judge_problems(_found(), expected) == []
    assert judge_problems(_found("a significant win"), expected) == [
        "unsupported: a significant win (Not stated.)"
    ]


def test_a_borderline_text_accepts_either_verdict() -> None:
    expected = JudgeExpectation(supported=None)

    assert judge_problems(_found(), expected) == []
    assert judge_problems(_found("his daughter Ava"), expected) == []


def test_an_entry_using_a_word_its_item_must_not_is_a_problem() -> None:
    expected = WRITTEN.model_copy(
        update={"items": [WRITTEN.items[0], WRITTEN.items[1].model_copy(update={"omit": ("CV",)})]}
    )
    thanks = THANKS.model_copy(
        update={"text": "Thank you to Sam Patel for covering the service desk, CV attached."}
    )

    problems = writer_problems(
        _draft([NORTHWIND], [thanks]),
        WRITER_TASK,
        expected,
        WRITER_EMAILS,
        ["customer_win", "shout_out"],
        400,
    )

    assert problems == ["m05: the entry says CV"]


def test_an_unsupported_text_must_name_the_words_that_break_it() -> None:
    expected = JudgeExpectation(supported=False, mention=("record-breaking",))

    assert judge_problems(_found("a record-breaking win"), expected) == []
    assert judge_problems(_found(), expected) == ["nothing found unsupported"]
    assert judge_problems(_found("on 25 September"), expected) == [
        "nothing unsupported names record-breaking"
    ]


def test_chosen_inputs_are_those_named_or_all_when_none_are() -> None:
    inputs = judge_inputs()

    assert chosen_inputs(inputs, None) == inputs
    assert [i.input_id for i in chosen_inputs(inputs, "notice-no-rates,intro")] == [
        "intro",
        "notice-no-rates",
    ]
    with pytest.raises(ValueError, match="no inputs named"):
        chosen_inputs(inputs, "intro,unknown")


def test_every_judge_text_has_its_items_and_an_expectation() -> None:
    inputs = judge_inputs()
    expected = corpus_expectation("judge")

    assert [i.input_id for i in inputs] == list(expected)
    for item in inputs:
        JudgeExpectation.model_validate(expected[item.input_id])
        assert len(item.task.items) == (7 if item.task.part == "intro" else 1)
        assert item.task.feedback


SHOW = ScenarioExpectation(
    drafted=True,
    message="Can I see the newsletter?",
    calls=("show_draft",),
    never=("write",),
    reply_lacks=("Thank you to Sam Patel",),
)


def test_a_turn_calling_the_expected_tools_in_order_has_no_problems() -> None:
    turn = Turn(tools=["get_newsletter", "show_draft"], reply="Here is version 1.")

    assert orchestrator_problems(turn, SHOW) == []


@pytest.mark.parametrize(
    ("turn", "problems"),
    [
        (
            Turn(tools=["get_newsletter"], reply="Done."),
            ["called ['get_newsletter'] instead of ['show_draft'] in order"],
        ),
        (Turn(tools=["show_draft", "write"], reply="Done."), ["called write"]),
        (
            Turn(tools=["show_draft"], reply="Thank you to Sam Patel for the cover."),
            ["the reply says Thank you to Sam Patel"],
        ),
        (
            Turn(
                tools=["show_draft"],
                reply="Your Customer Wins section, item-2, looks good \N{EM DASH} m05.",
            ),
            [
                "the reply contains Customer Wins",
                "the reply contains item-2",
                "the reply contains \N{EM DASH}",
                "the reply contains m05",
            ],
        ),
    ],
)
def test_each_departure_from_a_scenario_is_a_problem(turn: Turn, problems: list[str]) -> None:
    assert orchestrator_problems(turn, SHOW) == problems


def test_calling_a_tool_other_than_the_expected_times_is_a_problem() -> None:
    expected = ScenarioExpectation(drafted=False, message="Draft it.", times={"write": 2})

    assert orchestrator_problems(Turn(tools=["write", "write"], reply="Done."), expected) == []
    assert orchestrator_problems(Turn(tools=["write"] * 3, reply="Done."), expected) == [
        "called write 3 times instead of 2"
    ]


def test_section_names_other_than_the_titles_are_a_problem() -> None:
    turn = Turn(tools=["show_draft"], reply="Sections: Team news and Shout outs.")

    assert orchestrator_problems(turn, SHOW) == [
        "the reply contains Team news",
        "the reply contains Shout outs",
    ]
    titled = Turn(tools=["show_draft"], reply="Team news and new joiners, then Shout-outs.")
    assert orchestrator_problems(titled, SHOW) == []


def test_every_scenario_is_an_input() -> None:
    assert [i.input_id for i in orchestrator_inputs()] == list(orchestrator_expectations())


async def test_a_drafted_scenario_runs_the_orchestrator_on_version_1_with_stand_ins() -> None:
    config = load_config(Path("config/config.example.yaml"))
    seen: list[str] = []

    async def reviewer_facing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[-1]))
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("show_draft", {})])
        return ModelResponse(parts=[TextPart("Here is version 1.")])

    scenarios = OrchestratorScenarios(FunctionModel(reviewer_facing), config)

    turn = await scenarios.answer(Scenario(drafted=True, message="Can I see the newsletter?"))

    assert turn == Turn(tools=["show_draft"], reply="Here is version 1.")
    # The model saw the version the scripted run presented, through the real tools.
    assert '"version": 1' in seen[0] or "latest_version" in seen[0]


@pytest.mark.parametrize(
    ("fixes", "found"),
    [("clears", [(1, 0), (0, 1), (0, 0)]), ("sticks", [(1, 0), (0, 1), (0, 1)])],
)
async def test_a_fix_scenario_fails_the_check_then_the_judge_until_the_detail_clears(
    fixes: Literal["clears", "sticks"], found: list[tuple[int, int]]
) -> None:
    config = load_config(Path("config/config.example.yaml"))
    steps = ["start_newsletter", "extract", "consolidate"]
    steps += ["write", "judge", "write", "judge", "write", "judge", "present_draft"]
    calls = [
        ModelResponse(
            parts=[ToolCallPart(tool, {"instruction": "Fix it."} if tool == "write" else {})]
        )
        for tool in steps
    ]
    remaining = [*calls, ModelResponse(parts=[TextPart("Version 1 is presented.")])]
    returned: list[ToolReturnPart] = []

    async def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest):
            returned.extend(p for p in last.parts if isinstance(p, ToolReturnPart))
        return remaining.pop(0)

    scenarios = OrchestratorScenarios(FunctionModel(scripted), config)

    turn = await scenarios.answer(
        Scenario(drafted=False, fixes=fixes, message="Please draft a newsletter.")
    )

    assert turn.tools == steps
    judged = [part.model_response_object() for part in returned if part.tool_name == "judge"]
    # Check failures and unsupported texts each time the judge runs.
    assert [(j["failure_count"], len(j["unsupported"])) for j in judged] == found
