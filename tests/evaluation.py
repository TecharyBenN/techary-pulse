"""Live evaluation of a specialist agent on the corpus: its inputs, the answers the corpus expects,
scoring, and results saved with every request that produced them."""

import asyncio
import contextvars
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, Protocol, Self, get_args

import httpx2
from pydantic import AwareDatetime, BaseModel, TypeAdapter
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pulse.adapters.render import Renderer
from pulse.adapters.store import SqliteStore
from pulse.agents.consolidator.agent import ConsolidatorAgent, ConsolidatorTask
from pulse.agents.extractor.agent import ExtractorAgent
from pulse.agents.judge.agent import JudgeAgent, JudgeTask
from pulse.agents.orchestrator.agent import OrchestratorAgent
from pulse.agents.orchestrator.run import Orchestrator
from pulse.agents.orchestrator.tools import Tools
from pulse.agents.sensitivity.agent import SensitivityAgent
from pulse.agents.writer.agent import WriterAgent, WriterTask
from pulse.config import Config, load_config
from pulse.entities.base import Entity, StrictEntity
from pulse.entities.content import JudgeOutput, WriterOutput, check_content, item_sources
from pulse.entities.errors import SpecialistFailed
from pulse.entities.extracts import (
    Consolidation,
    ConsolidatorItem,
    ConsolidatorOutput,
    Extraction,
    ExtractorOutput,
    ExtractRecord,
    SensitivityKind,
    SensitivityOutput,
    with_sources,
)
from pulse.entities.mail import ScreenedEmail
from pulse.main import build_outbox
from pulse.services.operations import Operations
from tests.emails import (
    corpus_email,
    corpus_expectation,
    corpus_messages,
    graph_corpus_emails,
    screen_email,
)
from tests.fakes.clock import ControlledClock
from tests.fakes.mailbox import FakeMailbox
from tests.fakes.models import extractor_model, responses, sensitivity_model, text_response
from tests.messages import OPENED, make_message

EVALUATIONS = Path("evaluations")
# The included records the first-stage baselines produced, which the consolidator is given, and
# two of their excluded records as a reviewer restored them.
INCLUDED_RECORDS = Path(__file__).parent / "corpus" / "included_records.json"
_RECORDS = TypeAdapter(list[ExtractRecord])
# The items the consolidator baseline produced from those included records, and the excluded
# records, which the writer is given.
WRITER_INPUT = Path(__file__).parent / "corpus" / "writer_input.json"
# Texts from a tested writer draft, and three that each break one rule, which the judge is given.
JUDGE_INPUT = Path(__file__).parent / "corpus" / "judge_input.json"
# The writer's tested draft, which the writer stand-in returns to the orchestrator.
ORCHESTRATOR_INPUT = Path(__file__).parent / "corpus" / "orchestrator_input.json"
# The kind of instruction the orchestrator gives for a first draft.
FIRST_DRAFT = "Write the first draft of the newsletter."
# A reviewer's request for the newsletter, which the run records as feedback before the writer
# runs; only a scheduled start has none.
REQUEST = "Please draft a newsletter."

# The input and sample a request to the gateway belongs to; each evaluated call runs in its own
# task, so concurrent calls never share it.
_CALL: contextvars.ContextVar[tuple[str, int]] = contextvars.ContextVar("evaluated_call")


class Input[T](NamedTuple):
    """One task given to the agent, as Pulse gives it, named for the report, with where it came
    from."""

    input_id: str
    label: str
    # For a corpus email, "graph" when Graph returned it and "local" when it was kept out of the
    # dev inbox; for a later agent, the baselines its input came from.
    form: str
    task: T


def corpus_inputs() -> list[Input[ScreenedEmail]]:
    """Every corpus email that passes the pre-filter, in corpus order, named by corpus ID."""
    graph = {email.message_id: email for email in graph_corpus_emails()}
    inputs = []
    for position, message in enumerate(corpus_messages()):
        corpus_id = message["id"]
        if corpus_id in graph:
            email = graph[corpus_id]
            inputs.append(Input(corpus_id, email.subject, "graph", email))
        elif not message.get("seed", True):
            email = screen_email(corpus_email(message, position))
            if email.rejection is None:
                inputs.append(Input(corpus_id, email.subject, "local", email))
    return inputs


def consolidator_inputs() -> list[Input[ConsolidatorTask]]:
    """The newsletter's included records, as the first-stage baselines produced them, each named by
    the corpus ID of its email; then the same with the restored records among them, in corpus
    order, as the store lists them."""
    saved = json.loads(INCLUDED_RECORDS.read_text(encoding="utf-8"))
    included = _RECORDS.validate_python(saved["records"])
    restored = _RECORDS.validate_python(saved["restored"])
    order = [message["id"] for message in corpus_messages()]
    with_restored = sorted([*included, *restored], key=lambda r: order.index(r.message_id))
    return [
        Input(
            "included",
            f"{len(included)} included records",
            "baselines",
            ConsolidatorTask(records=included),
        ),
        Input(
            "restored",
            f"{len(included)} included and {len(restored)} restored records",
            "baselines",
            ConsolidatorTask(records=with_restored),
        ),
    ]


def writer_inputs() -> list[Input[WriterTask]]:
    """A first draft a reviewer asked for, from the consolidator baseline's items, with the
    sender names and received times of their corpus emails, and the excluded records, as `write`
    gives them."""
    saved = json.loads(WRITER_INPUT.read_text(encoding="utf-8"))
    consolidation = Consolidation.model_validate(saved["consolidation"])
    emails = [item.task for item in corpus_inputs()]
    task = WriterTask(
        headline=consolidation.headline,
        items=with_sources(consolidation.items, emails),
        excluded=_RECORDS.validate_python(saved["excluded"]),
        feedback=[make_message(text=REQUEST)],
        instruction=FIRST_DRAFT,
        draft=None,
    )
    label = f"first draft of {len(consolidation.items)} items"
    return [Input("first-draft", label, "baselines", task)]


def judge_inputs() -> list[Input[JudgeTask]]:
    """Each text with what `judge` gives it: every item for the intro, its own for an entry, and
    the writer input's feedback."""
    [written] = writer_inputs()
    items, feedback = written.task.items, written.task.feedback
    inputs = []
    for case in json.loads(JUDGE_INPUT.read_text(encoding="utf-8"))["cases"]:
        own = [i for i in items if set(i.source_message_ids) == set(case["sources"] or [])]
        task = JudgeTask(
            part=case["part"],
            text=case["text"],
            items=items if case["part"] == "intro" else own,
            feedback=feedback,
        )
        form = "planted" if case["id"].startswith("planted") else "baselines"
        inputs.append(Input(case["id"], case["text"][:60], form, task))
    return inputs


class ExtractorExpectation(StrictEntity):
    """The extractor's right answer for one email, written from the design."""

    category: str | None
    # None when facts are optional: either answer is reliable enough, and one with no facts names
    # no one.
    has_facts: bool | None
    people: list[str]
    # Words the facts must contain, and words they must not.
    mention: tuple[str, ...] = ()
    omit: tuple[str, ...] = ()


def expectations[E: BaseModel](agent: str, model: type[E]) -> dict[str, E]:
    """The test's expected answers for an agent that reads one email, by corpus ID."""
    return {
        m["id"]: model.model_validate(m["expect"][agent])
        for m in corpus_messages()
        if agent in m.get("expect", {})
    }


def extractor_problems(output: ExtractorOutput, expected: ExtractorExpectation) -> list[str]:
    """How the answer departs from the expected one; empty when it is right."""
    problems = []
    if output.category != expected.category:
        problems.append(f"category {output.category} instead of {expected.category}")
    if expected.has_facts is not None and bool(output.facts) != expected.has_facts:
        problems.append(
            "facts given for an email with no news"
            if output.facts
            else "no facts for an email with news"
        )
    left_out = expected.has_facts is None and not output.facts
    people = [] if left_out else expected.people
    if {_words(name) for name in output.people} != {_words(name) for name in people}:
        problems.append(f"people {output.people} instead of {people}")
    mention = () if left_out else expected.mention
    problems += [f"the facts do not mention {word}" for word in _missing(mention, output.facts)]
    problems += [
        f"the facts mention {word}"
        for word in expected.omit
        if word not in _missing([word], output.facts)
    ]
    return problems


class SensitivityExpectation(StrictEntity):
    """The sensitivity agent's right answer for one email: each flag kind it raises, and whether
    that kind is withheld."""

    # None for a kind that may be raised or not, withheld or not: either answer is reliable
    # enough, so it never decides whether the email is withheld.
    flags: dict[SensitivityKind, bool | None]


def sensitivity_problems(output: SensitivityOutput, expected: SensitivityExpectation) -> list[str]:
    """How the flags depart from the expected ones; empty when they are right. The withheld
    decision, which keeps the email out of the draft, comes first. A kind is withheld when any of
    its flags is."""
    given: dict[str, bool] = {}
    for flag in output.sensitivity:
        given[flag.kind] = given.get(flag.kind, False) or flag.withheld
    problems = []
    withheld = any(given.values())
    if withheld != any(value is True for value in expected.flags.values()):
        problems.append(
            "withheld instead of included" if withheld else "included instead of withheld"
        )
    for kind in get_args(SensitivityKind):
        if kind not in expected.flags:
            if kind in given:
                problems.append(f"an extra {kind} flag")
            continue
        wanted = expected.flags[kind]
        if wanted is None:
            continue
        if kind not in given:
            problems.append(f"no {kind} flag")
        elif given[kind] != wanted:
            problems.append(f"{kind} {_held(given[kind])} instead of {_held(wanted)}")
    return problems


def _held(withheld: bool) -> str:
    return "withheld" if withheld else "not withheld"


class ItemExpectation(StrictEntity):
    """One item the consolidator should return: the corpus emails merged into it."""

    sources: list[str]
    category: str
    # Words the item's facts must contain, and words the writer's entry for it must not.
    mention: tuple[str, ...] = ()
    omit: tuple[str, ...] = ()


class ConsolidatorExpectation(StrictEntity):
    """The consolidator's right answer for the newsletter's included records."""

    items: list[ItemExpectation]


def consolidator_expectations() -> dict[str, ConsolidatorExpectation]:
    """The expected items for each consolidator input: with restored records, the included
    records' items and one more for each restored record."""
    expected = corpus_expectation("consolidator")
    items = [ItemExpectation.model_validate(item) for item in expected["items"]]
    restored = [ItemExpectation.model_validate(item) for item in expected["restored_items"]]
    return {
        "included": ConsolidatorExpectation(items=items),
        "restored": ConsolidatorExpectation(items=items + restored),
    }


def consolidator_problems(
    output: ConsolidatorOutput, sources: Mapping[str, str], expected: ConsolidatorExpectation
) -> list[str]:
    """How the items depart from the expected ones; empty when they are right. `sources` maps
    each input record ID to the corpus ID of its email."""
    given = {
        frozenset(sources[record_id] for record_id in item.source_record_ids): item
        for item in output.items
    }
    problems = []
    for wanted in expected.items:
        name = " + ".join(wanted.sources)
        item = given.pop(frozenset(wanted.sources), None)
        if item is None:
            problems.append(f"no item merging {name}")
            continue
        if item.category != wanted.category:
            problems.append(f"{name}: category {item.category} instead of {wanted.category}")
        problems += [
            f"{name}: the facts do not mention {word}"
            for word in _missing(wanted.mention, item.facts)
        ]
    problems += [f"an unexpected item merging {' + '.join(sorted(merged))}" for merged in given]
    return problems


def writer_problems(
    output: WriterOutput,
    task: WriterTask,
    expected: ConsolidatorExpectation,
    emails: Sequence[ScreenedEmail],
    categories: Collection[str],
    max_words: int,
) -> list[str]:
    """How the draft departs from the expected one; empty when it is right. Pulse's own draft
    checks come first; then, for each expected item, its entry, its section, the facts it must
    keep and any sender it names who only shared it."""
    content = output.content
    sources = item_sources(content, task.items, emails)
    feedback = [message.text for message in task.feedback]
    problems = [
        f"{failure.check} check failed at {failure.target or 'the whole draft'}: {failure.detail}"
        for failure in check_content(content, sources, feedback, categories, max_words)
    ]
    placed = {
        entry.item_id: (section.category, entry)
        for section in content.sections
        for entry in section.entries
    }
    by_sources = {frozenset(item.source_message_ids): item for item in task.items}
    for wanted in expected.items:
        name = " + ".join(wanted.sources)
        item = by_sources[frozenset(wanted.sources)]
        # The items check only compares entries with the draft's own item IDs.
        if item.item_id not in placed:
            problems.append(f"no entry for {name}")
            continue
        category, entry = placed[item.item_id]
        if category != item.category:
            problems.append(f"{name}: in {category} instead of {item.category}")
        problems += [
            f"{name}: the entry does not mention {word}"
            for word in _missing(wanted.mention, [entry.text])
        ]
        problems += [
            f"{name}: the entry says {word}"
            for word in wanted.omit
            if not _missing([word], [entry.text])
        ]
        people = {person.casefold() for person in item.people}
        problems += [
            f"{name}: the entry names {sender}, who only shared it"
            for sender in item.sender_names
            if sender.casefold() not in people and not _missing([sender], [entry.text])
        ]
    return problems


class JudgeExpectation(StrictEntity):
    """The judge\'s right answer for one text: whether it keeps to its facts, and words what it
    finds unsupported must name when it does not."""

    # None for a borderline text, where either verdict is acceptable.
    supported: bool | None
    mention: tuple[str, ...] = ()


def judge_problems(output: JudgeOutput, expected: JudgeExpectation) -> list[str]:
    """How the verdict departs from the expected one; empty when it is right."""
    unsupported = [part.text for part in output.unsupported]
    if expected.supported is None:
        return []
    if expected.supported:
        return [f"unsupported: {part.text} ({part.reason})" for part in output.unsupported]
    if not unsupported:
        return ["nothing found unsupported"]
    return [f"nothing unsupported names {word}" for word in _missing(expected.mention, unsupported)]


def _missing(words: Sequence[str], facts: Sequence[str]) -> list[str]:
    """The words that no fact contains as whole words, ignoring case, punctuation and spacing."""
    padded = [f" {_words(fact)} " for fact in facts]
    return [word for word in words if not any(f" {_words(word)} " in fact for fact in padded)]


def _words(text: str) -> str:
    """The text's words, lower case, separated by single spaces."""
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


class Exchange(Entity):
    """One request to the gateway, named by the hash of its body, and the response to it."""

    request_sha256: str
    status: int
    model: str | None
    finish_reason: str | None
    usage: dict[str, Any] | None
    # The model's answer, or the error body when the request failed.
    content: str | None


class Recorder:
    """Records every request sent to the gateway and its response, by the call they belong to.
    Headers are never recorded, so neither is the gateway key."""

    def __init__(self) -> None:
        self._exchanges: defaultdict[tuple[str, int], list[Exchange]] = defaultdict(list)
        # Each distinct request body, by the hash of its bytes.
        self.requests: dict[str, Any] = {}

    def client(self, **kwargs: Any) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(event_hooks={"response": [self._record]}, **kwargs)

    def take(self, input_id: str, sample: int) -> list[Exchange]:
        return self._exchanges.pop((input_id, sample), [])

    async def _record(self, response: httpx2.Response) -> None:
        await response.aread()
        body = response.request.content
        digest = hashlib.sha256(body).hexdigest()
        self.requests.setdefault(digest, json.loads(body))
        exchange = Exchange(
            request_sha256=digest,
            status=response.status_code,
            model=None,
            finish_reason=None,
            usage=None,
            content=response.text,
        )
        if response.is_success:
            completion = response.json()
            [choice] = completion["choices"]
            exchange = exchange.model_copy(
                update={
                    "model": completion.get("model"),
                    "finish_reason": choice.get("finish_reason"),
                    "usage": completion.get("usage"),
                    "content": choice["message"].get("content"),
                }
            )
        self._exchanges[_CALL.get()].append(exchange)


class Answer(Entity):
    """One sample of the agent's answer for one input, scored, with the exchanges behind it."""

    input_id: str
    form: str
    sample: int
    output: dict[str, Any] | None
    # Why the agent gave no valid answer, when it gave none.
    error: str | None
    problems: list[str]
    exchanges: list[Exchange]


async def evaluate[T, O: BaseModel](
    answer: Callable[[T], Awaitable[O]],
    problems: Callable[[str, O], list[str]],
    inputs: Sequence[Input[T]],
    samples: int,
    recorder: Recorder,
) -> list[Answer]:
    """Answer every input `samples` times. Each round answers every input at once, as `extract`
    does; `problems` scores an answer for the input named by its ID."""

    async def one(item: Input[T], sample: int) -> Answer:
        _CALL.set((item.input_id, sample))
        output: O | None = None
        try:
            output = await answer(item.task)
        except (SpecialistFailed, AgentRunError) as failure:
            error: str | None = str(failure)
            found = [f"no valid response: {failure}"]
        else:
            error = None
            found = problems(item.input_id, output)
        return Answer(
            input_id=item.input_id,
            form=item.form,
            sample=sample,
            output=None if output is None else output.model_dump(mode="json"),
            error=error,
            problems=found,
            exchanges=recorder.take(item.input_id, sample),
        )

    answers: list[Answer] = []
    for sample in range(1, samples + 1):
        answers += await asyncio.gather(*(one(item, sample) for item in inputs))
    return answers


class Results(Entity):
    """An evaluation run's answers with what produced them: the code, the model and every
    request body sent."""

    agent: str
    model: str
    base_url: str
    commit: str
    # `git status --porcelain` lines, so changes not yet committed are known too.
    uncommitted: list[str]
    started: AwareDatetime
    finished: AwareDatetime
    samples: int
    answers: list[Answer]
    requests: dict[str, Any]

    @classmethod
    def of(
        cls,
        *,
        agent: str,
        model: str,
        base_url: str,
        samples: int,
        started: AwareDatetime,
        finished: AwareDatetime,
        answers: list[Answer],
        recorder: Recorder,
    ) -> Self:
        return cls(
            agent=agent,
            model=model,
            base_url=base_url,
            commit=_git("rev-parse", "HEAD").strip(),
            uncommitted=_git("status", "--porcelain").splitlines(),
            started=started,
            finished=finished,
            samples=samples,
            answers=answers,
            requests=recorder.requests,
        )


def save_results(results: Results, folder: Path = EVALUATIONS) -> Path:
    """Write the results to `folder`/agent/start time.json, readable only by their owner."""
    directory = folder / results.agent
    for path in (folder, directory):
        if not path.exists():
            path.mkdir()
            # mkdir applies the umask, so the mode is set afterwards.
            path.chmod(0o700)
    path = directory / f"{results.started.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(results.model_dump_json(indent=2))
    return path


class Answering[T, O](Protocol):
    """An agent, or a stand-in Pulse around one, answering one task."""

    async def answer(self, task: T) -> O: ...


async def run_live[T, O: BaseModel](
    agent: str,
    build: Callable[[Model, Config], Answering[T, O]],
    inputs: Sequence[Input[T]],
    problems: Callable[[str, O], list[str]],
    details: Callable[[dict[str, Any]], list[str]],
    samples: int,
) -> list[Answer]:
    """Run the agent, built by `build` as main builds it, on every input through the dev gateway,
    using ./config.yaml and .env. Save the results and print a report for each input; `details`
    gives the lines worth reading from one answer."""
    config = load_config(Path("config.yaml"))
    model = config.llm.models[agent]
    recorder = Recorder()
    provider = OpenAIProvider(
        base_url=config.llm.base_url,
        api_key=os.environ[config.llm.api_key_env],
        http_client=recorder.client(),
    )
    evaluated = build(OpenAIChatModel(model, provider=provider), config)
    started = datetime.now(UTC)
    answers = await evaluate(evaluated.answer, problems, inputs, samples, recorder)
    results = Results.of(
        agent=agent,
        model=model,
        base_url=config.llm.base_url,
        samples=samples,
        started=started,
        finished=datetime.now(UTC),
        answers=answers,
        recorder=recorder,
    )
    path = save_results(results)
    for item in inputs:
        _report(item, [a for a in answers if a.input_id == item.input_id], details)
    calls = sum(len(answer.exchanges) for answer in answers)
    input_tokens, output_tokens = token_totals(answers)
    right = sum(1 for answer in answers if not answer.problems)
    print(
        f"\n{right}/{len(answers)} answers right; {calls} calls, "
        f"{input_tokens} input and {output_tokens} output tokens; results in {path}"
    )
    return answers


def token_totals(answers: Sequence[Answer]) -> tuple[int, int]:
    """The input and output tokens of every exchange; a failed request reports none."""
    usages = [exchange.usage or {} for answer in answers for exchange in answer.exchanges]
    return (
        sum(usage.get("prompt_tokens", 0) for usage in usages),
        sum(usage.get("completion_tokens", 0) for usage in usages),
    )


def _report[T](
    item: Input[T], answers: list[Answer], details: Callable[[dict[str, Any]], list[str]]
) -> None:
    right = sum(1 for answer in answers if not answer.problems)
    retried = sum(1 for answer in answers if len(answer.exchanges) > 1)
    heading = f"{item.input_id} {item.label!r}"
    print(f"\n{heading}: {right}/{len(answers)} right, {retried} retried")
    lines = [problem for answer in answers for problem in answer.problems]
    lines += [line for answer in answers if answer.output for line in details(answer.output)]
    for line, count in Counter(lines).items():
        print(f"    {count}x {line}")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


class Scenario(Entity):
    """A reviewer's message to the orchestrator, sent to a new newsletter, or to one whose version 1
    a scripted run has already drafted and presented."""

    drafted: bool
    message: str
    # Whether Pulse uses the example configuration's scheduled send rule in place of
    # ./config.yaml's.
    scheduled_send: bool = False


class Turn(Entity):
    """The tools the orchestrator called for the message, in order, and its reply."""

    tools: list[str]
    reply: str | None


class ScenarioExpectation(Scenario, StrictEntity):
    """The orchestrator's right answer to a scenario: tools it must call in this order, with any
    others between, tools it must not call, and what its reply must and must not say."""

    calls: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    reply_has: tuple[str, ...] = ()
    reply_lacks: tuple[str, ...] = ()


def orchestrator_expectations() -> dict[str, ScenarioExpectation]:
    return {
        scenario_id: ScenarioExpectation.model_validate(answer)
        for scenario_id, answer in corpus_expectation("orchestrator").items()
    }


def orchestrator_inputs() -> list[Input[Scenario]]:
    return [
        Input(
            scenario_id,
            expected.message,
            "scenario",
            Scenario(**expected.model_dump(include=set(Scenario.model_fields))),
        )
        for scenario_id, expected in orchestrator_expectations().items()
    ]


# The example configuration, whose send rule is the scheduled one production uses.
_EXAMPLE = load_config(Path(__file__).parent.parent / "config" / "config.example.yaml")

# What no reply may contain: IDs, dashes, and sections named by category rather than title.
_REPLY_NEVER = re.compile(
    r"\b(?:item|record|excluded)-\d+\b|\bm\d\d\b|[\u2013\u2014]"
    r"|customer_win|delivery_highlight|team_news|shout_out|company_notices"
    r"|Customer Wins?\b|Delivery Highlights?\b|Team News\b|Shout Outs?\b|Company Notices?\b"
    r"|(?i:\bshout outs?\b)|(?i:\bteam news\b(?! and new joiners))"
)


def orchestrator_problems(turn: Turn, expected: ScenarioExpectation) -> list[str]:
    """How the turn departs from the expected one; empty when it is right."""
    problems = []
    remaining = iter(turn.tools)
    if not all(tool in remaining for tool in expected.calls):
        problems.append(f"called {turn.tools} instead of {list(expected.calls)} in order")
    problems += [f"called {tool}" for tool in expected.never if tool in turn.tools]
    reply = turn.reply or ""
    problems += [
        f"the reply does not say {text}"
        for text in expected.reply_has
        if text.casefold() not in reply.casefold()
    ]
    problems += [
        f"the reply says {text}"
        for text in expected.reply_lacks
        if text.casefold() in reply.casefold()
    ]
    problems += [
        f"the reply contains {match}" for match in dict.fromkeys(_REPLY_NEVER.findall(reply))
    ]
    return problems


class OrchestratorScenarios:
    """A fresh Pulse for each scenario, with the given orchestrator model and every specialist agent
    replaced by a stand-in answering with its baseline, so only the orchestrator varies."""

    def __init__(self, model: Model, config: Config) -> None:
        self._model = model
        self._config = config

    async def answer(self, scenario: Scenario) -> Turn:
        config = self._config
        with tempfile.TemporaryDirectory() as folder:
            store = SqliteStore(Path(folder) / "pulse.db")
            await store.initialise()
            inbox = [corpus_email(message, n) for n, message in enumerate(corpus_messages())]
            renderer = Renderer(config.timezone)
            operations = Operations(
                store,
                FakeMailbox(inbox),
                build_outbox(config, FakeMailbox(), store, renderer),
                screen_email,
                renderer.reviewer_email,
                ControlledClock(OPENED),
                send_rule=_EXAMPLE.send if scenario.scheduled_send else config.send,
                timezone=config.timezone,
                section_titles=config.section_titles,
                max_words=config.limits.max_words,
            )
            tools = Tools(operations, store, *_standin_agents(config))
            lock = asyncio.Lock()

            def orchestrator(model: Model) -> Orchestrator:
                return Orchestrator(
                    OrchestratorAgent(model, tools.toolset()),
                    store,
                    operations.summary,
                    config.orchestrator.max_tool_calls,
                    timedelta(minutes=config.orchestrator.max_run_minutes),
                    lock,
                )

            if scenario.drafted:
                scripted = FunctionModel(_scripted_draft())
                await orchestrator(scripted).handle(
                    make_message("s01", text="Please draft a newsletter.")
                )
            run = await orchestrator(self._model).handle(make_message("r02", text=scenario.message))
            newsletter = await store.get_latest_newsletter()
            rows = await store.load_history(newsletter.newsletter_id) if newsletter else []
            called = [
                part.tool_name
                for row in rows
                if row.message_id == "r02"
                for step in ModelMessagesTypeAdapter.validate_json(row.data)
                if isinstance(step, ModelResponse)
                for part in step.parts
                if isinstance(part, ToolCallPart)
            ]
            return Turn(tools=called, reply=run.text)


def _scripted_draft() -> Callable[[list[ModelMessage], AgentInfo], Awaitable[ModelResponse]]:
    """The orchestrator's draft run as its instructions lay it out, so a scenario that starts from
    version 1 always starts from the same history."""
    steps = ["get_newsletter", "start_newsletter", "extract", "consolidate"]
    calls = [ModelResponse(parts=[ToolCallPart(tool, {})]) for tool in steps]
    calls.append(ModelResponse(parts=[ToolCallPart("write", {"instruction": FIRST_DRAFT})]))
    calls += [
        ModelResponse(parts=[ToolCallPart(tool, {})])
        for tool in ("check", "judge", "present_draft")
    ]
    reply = text_response("I have drafted the newsletter and sent version 1 to the reviewers.")
    return responses(*calls, reply)


def _standin_agents(
    config: Config,
) -> tuple[ExtractorAgent, SensitivityAgent, ConsolidatorAgent, WriterAgent, JudgeAgent]:
    """The specialist agents, each answering with its baseline: the first-stage records, the
    consolidator's items, the writer's tested draft, and no unsupported text."""
    records = _RECORDS.validate_python(
        json.loads(INCLUDED_RECORDS.read_text(encoding="utf-8"))["records"]
        + json.loads(WRITER_INPUT.read_text(encoding="utf-8"))["excluded"]
    )
    bodies = {message["id"]: message["body"] for message in corpus_messages()}
    fields = set(Extraction.model_fields)
    extractions = {
        bodies[record.message_id]: Extraction.model_validate(record.model_dump(include=fields))
        for record in records
    }
    consolidation = Consolidation.model_validate(
        json.loads(WRITER_INPUT.read_text(encoding="utf-8"))["consolidation"]
    )
    by_facts = {tuple(record.facts): record.message_id for record in records}
    draft = WriterOutput.model_validate(
        json.loads(ORCHESTRATOR_INPUT.read_text(encoding="utf-8"))["draft"]
    )

    async def consolidator(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        given = json.loads(_prompt(messages).split("\n")[1])
        record_ids = {by_facts[tuple(record["facts"])]: record["record_id"] for record in given}
        items = [
            ConsolidatorItem(
                category=item.category,
                facts=item.facts,
                source_record_ids=[record_ids[m] for m in item.source_message_ids],
            )
            for item in consolidation.items
        ]
        output = ConsolidatorOutput(headline=consolidation.headline, items=items)
        return text_response(output.model_dump_json())

    async def writer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "<working_draft>" not in _prompt(messages):
            return text_response(draft.model_dump_json())
        content = draft.content.model_copy(
            update={"intro": "A good week for customers and the team."}
        )
        revised = WriterOutput(content=content, changes=["Shortened the intro."], not_applied=[])
        return text_response(revised.model_dump_json())

    async def judge(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return text_response(JudgeOutput(unsupported=[]).model_dump_json())

    return (
        ExtractorAgent(extractor_model(extractions), config.categories),
        SensitivityAgent(sensitivity_model(extractions)),
        ConsolidatorAgent(FunctionModel(consolidator), config.categories),
        WriterAgent(
            FunctionModel(writer),
            config.categories,
            config.section_titles,
            config.headline_title,
            config.limits.max_words,
        ),
        JudgeAgent(FunctionModel(judge)),
    )


def _prompt(messages: list[ModelMessage]) -> str:
    request = messages[0]
    assert isinstance(request, ModelRequest)
    [prompt] = [part.content for part in request.parts if isinstance(part, UserPromptPart)]
    assert isinstance(prompt, str)
    return prompt
