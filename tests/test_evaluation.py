import json
import stat
from collections.abc import Mapping
from pathlib import Path

import httpx2
import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pulse.agents.extractor.agent import ExtractorAgent
from pulse.entities.extracts import ExtractorOutput
from tests.emails import corpus_email, corpus_messages, make_output, make_screened_email, share
from tests.emails import screen_email as screened
from tests.evaluation import (
    ExtractorExpectation,
    Input,
    Recorder,
    Results,
    corpus_inputs,
    evaluate,
    extractor_expectations,
    extractor_problems,
    save_results,
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

    assert sorted(i.corpus_id for i in inputs) == sorted(passing)
    assert set(extractor_expectations()) == passing
    # Only emails kept out of the dev inbox are built locally; the rest are as Graph returned them.
    local = {m["id"] for m in messages if not m.get("seed", True)}
    assert {i.corpus_id for i in inputs if i.form == "local"} == local & passing


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
    Input("m01", "graph", make_screened_email("m01")),
    Input("m02", "local", make_screened_email("m02", body="Tom Evans closed Contoso today.")),
]


async def test_evaluate_scores_every_sample_with_the_exchanges_that_produced_it() -> None:
    recorder = Recorder()
    agent = _gateway(recorder)

    answers = await evaluate(
        agent.answer, lambda _, output: extractor_problems(output, EXPECTED), INPUTS, 2, recorder
    )

    assert [(a.corpus_id, a.form, a.sample) for a in answers] == [
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
        c: {a.exchanges[0].request_sha256 for a in answers if a.corpus_id == c}
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

    exchanges = {a.corpus_id: len(a.exchanges) for a in answers}
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
