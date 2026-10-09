"""Live evaluation of a specialist agent on the corpus: its inputs, the answers the corpus expects,
scoring, and results saved with every request that produced them."""

import asyncio
import contextvars
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC
from pathlib import Path
from typing import Any, Literal, NamedTuple, Self

import httpx2
from pydantic import AwareDatetime, BaseModel
from pydantic_ai.exceptions import AgentRunError

from pulse.entities.base import Entity, StrictEntity
from pulse.entities.errors import SpecialistFailed
from pulse.entities.extracts import ExtractorOutput
from pulse.entities.mail import ScreenedEmail
from tests.emails import corpus_email, corpus_messages, graph_corpus_emails, screen_email

EVALUATIONS = Path("evaluations")

# The email and sample a request to the gateway belongs to; each evaluated call runs in its own
# task, so concurrent calls never share it.
_CALL: contextvars.ContextVar[tuple[str, int]] = contextvars.ContextVar("evaluated_call")


class Input(NamedTuple):
    """A corpus email given to the agent: as Graph returned it, or built locally when it was kept
    out of the dev inbox."""

    corpus_id: str
    form: Literal["graph", "local"]
    email: ScreenedEmail


def corpus_inputs() -> list[Input]:
    """Every corpus email that passes the pre-filter, in corpus order."""
    graph = {email.message_id: email for email in graph_corpus_emails()}
    inputs = []
    for position, message in enumerate(corpus_messages()):
        corpus_id = message["id"]
        if corpus_id in graph:
            inputs.append(Input(corpus_id, "graph", graph[corpus_id]))
        elif not message.get("seed", True):
            email = screen_email(corpus_email(message, position))
            if email.rejection is None:
                inputs.append(Input(corpus_id, "local", email))
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


def extractor_expectations() -> dict[str, ExtractorExpectation]:
    return {
        m["id"]: ExtractorExpectation.model_validate(m["expect"]["extractor"])
        for m in corpus_messages()
        if "extractor" in m.get("expect", {})
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
    facts = [f" {_words(fact)} " for fact in output.facts]
    problems += [
        f"the facts do not mention {word}"
        for word in ([] if left_out else expected.mention)
        if not any(f" {_words(word)} " in fact for fact in facts)
    ]
    problems += [
        f"the facts mention {word}"
        for word in expected.omit
        if any(f" {_words(word)} " in fact for fact in facts)
    ]
    return problems


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

    def take(self, corpus_id: str, sample: int) -> list[Exchange]:
        return self._exchanges.pop((corpus_id, sample), [])

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
    """One sample of the agent's answer for one email, scored, with the exchanges behind it."""

    corpus_id: str
    form: Literal["graph", "local"]
    sample: int
    output: dict[str, Any] | None
    # Why the agent gave no valid answer, when it gave none.
    error: str | None
    problems: list[str]
    exchanges: list[Exchange]


async def evaluate[O: BaseModel](
    answer: Callable[[ScreenedEmail], Awaitable[O]],
    problems: Callable[[str, O], list[str]],
    inputs: Sequence[Input],
    samples: int,
    recorder: Recorder,
) -> list[Answer]:
    """Answer every input `samples` times. Each round answers every input at once, as `extract`
    does; `problems` scores an answer for the email named by its corpus ID."""

    async def one(item: Input, sample: int) -> Answer:
        _CALL.set((item.corpus_id, sample))
        output: O | None = None
        try:
            output = await answer(item.email)
        except (SpecialistFailed, AgentRunError) as failure:
            error: str | None = str(failure)
            found = [f"no valid response: {failure}"]
        else:
            error = None
            found = problems(item.corpus_id, output)
        return Answer(
            corpus_id=item.corpus_id,
            form=item.form,
            sample=sample,
            output=None if output is None else output.model_dump(mode="json"),
            error=error,
            problems=found,
            exchanges=recorder.take(item.corpus_id, sample),
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


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
