"""The entry point for every channel: one reviewer message in, the reply or no reply out."""

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from pydantic_ai import Agent, AgentRun, CallToolsNode, UsageLimits
from pydantic_ai.exceptions import AgentRunError, UnexpectedModelBehavior
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
)

from pulse.agents.orchestrator.agent import NO_REPLY, user_prompt
from pulse.agents.orchestrator.tools import ShowResult, Tools, progress_note
from pulse.agents.runner import is_truncated_or_refused
from pulse.entities.base import Entity
from pulse.entities.content import Content
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import PulseError, RunFailed
from pulse.entities.store import DELIVERY_NOTE, Store

Progress = Callable[[str], Awaitable[None]]

_PRESENT_DRAFT = Tools.present_draft.__name__
# Each of these tools shows the reviewer a newsletter after the reply.
_SHOWING = (_PRESENT_DRAFT, Tools.show_draft.__name__)

_log = logging.getLogger(__name__)


class RunReply(Entity):
    """The reply, or None when the message needs none, and the newsletter the run presented or
    showed, so the channel can show it in its own format."""

    text: str | None
    newsletter: Content | None


async def _no_progress(note: str) -> None:
    """For channels that show no progress notes."""


class _History:
    """The latest newsletter's conversation history, saving each step as it is added.

    Before any newsletter exists, the history starts empty and is held. When a run opens a
    newsletter, the run's exchange so far becomes the new newsletter's history, and its message
    the new newsletter's feedback; the earlier newsletter keeps what was saved before.
    """

    def __init__(self, store: Store, newsletter_id: str | None) -> None:
        self._store = store
        self.newsletter_id = newsletter_id
        # What the model sees, with superseded results replaced by placeholders.
        self.messages: list[ModelMessage] = []
        # Every step as saved, with the message whose run produced it.
        self._steps: list[tuple[str, ModelMessage]] = []
        # The messages recorded as feedback for the newsletter the history belongs to.
        self._recorded: set[str] = set()
        # The message whose run saved the latest step.
        self.owner: str | None = None
        # The latest newsletter each message's run presented or showed, by message ID.
        self.shown: dict[str, ShowResult] = {}

    async def load(self, message: ReviewerMessage) -> None:
        """Record the message as feedback, then load the saved steps."""
        if self.newsletter_id is None:
            return
        await self._record(message)
        rows = await self._store.load_history(self.newsletter_id)
        self._steps = [
            (row.message_id, m)
            for row in rows
            for m in ModelMessagesTypeAdapter.validate_json(row.data)
        ]
        for message_id, step in self._steps:
            self._note(message_id, step)
        self.messages = _superseded_results_replaced([step for _, step in self._steps])
        self.owner = rows[-1].message_id if rows else None

    async def recorded(self, message_id: str) -> ReviewerMessage:
        """The recorded reviewer message whose run saved the given steps."""
        feedback = await self._store.list_feedback(self.newsletter_id) if self.newsletter_id else []
        recorded = next((m for m in feedback if m.message_id == message_id), None)
        if recorded is None:
            raise RunFailed(f"message {message_id} has saved steps but is not recorded")
        return recorded

    def unfinished(self) -> bool:
        """Whether the latest run stopped before its final response; delivery's note is not a
        run."""
        last = self.messages[-1] if self.messages else None
        return self.owner != DELIVERY_NOTE and (
            isinstance(last, ModelRequest)
            or (isinstance(last, ModelResponse) and bool(last.tool_calls))
        )

    async def add(self, message: ReviewerMessage, step: ModelMessage) -> None:
        """Add a step of the message's run, saving it to the latest newsletter."""
        message_id = message.message_id
        self.messages.append(step)
        self._steps.append((message_id, step))
        self.owner = message_id
        self._note(message_id, step)
        latest = await self._store.get_latest_newsletter()
        if latest is None:
            return
        if latest.newsletter_id != self.newsletter_id:
            # The run opened a newsletter, so its exchange so far starts the new history.
            self.newsletter_id = latest.newsletter_id
            self._recorded = set()
            await self._record(message)
            run = [s for owner, s in self._steps if owner == message_id]
            await self._save(latest.newsletter_id, message_id, run)
            return
        await self._record(message)
        await self._save(latest.newsletter_id, message_id, [step])

    async def _record(self, message: ReviewerMessage) -> None:
        if self.newsletter_id is not None and message.message_id not in self._recorded:
            await self._store.record_feedback(self.newsletter_id, message)
            self._recorded.add(message.message_id)

    def _note(self, message_id: str, message: ModelMessage) -> None:
        if shown := _results(message, _SHOWING):
            # During a run a result is the tool's own model; loaded from the store, it is a dict.
            self.shown[message_id] = ShowResult.model_validate(shown[-1], from_attributes=True)

    async def _save(
        self, newsletter_id: str, message_id: str, steps: Sequence[ModelMessage]
    ) -> None:
        for step in steps:
            data = ModelMessagesTypeAdapter.dump_json([step])
            await self._store.append_history(newsletter_id, message_id, data)


class Orchestrator:
    """Runs the orchestrator for each reviewer message, one run at a time, in arrival order."""

    def __init__(
        self,
        agent: Agent[ReviewerMessage, str],
        store: Store,
        max_tool_calls: int,
        max_run_time: timedelta,
        lock: asyncio.Lock,
    ) -> None:
        """`lock` is the run lock, which delivery also takes, so a run and a delivery never
        change a newsletter at the same time."""
        self._agent = agent
        self._store = store
        self._max_tool_calls = max_tool_calls
        self._max_run_time = max_run_time
        # asyncio.Lock wakes its waiters first in, first out, which keeps arrival order.
        self._lock = lock

    async def handle(
        self, message: ReviewerMessage, on_progress: Progress = _no_progress
    ) -> RunReply:
        """Raise RunFailed on failure."""
        async with self._lock:
            started = time.monotonic()
            newsletter = await self._store.get_latest_newsletter()
            newsletter_id = newsletter.newsletter_id if newsletter else None
            fields: dict[str, object] = {
                "conversation_id": newsletter_id,
                "message_id": message.message_id,
                "channel": message.channel,
            }
            history = _History(self._store, newsletter_id)
            try:
                async with asyncio.timeout(self._max_run_time.total_seconds()):
                    reply = await self._converse(message, history, on_progress)
            except (AgentRunError, TimeoutError, PulseError) as error:
                fields["conversation_id"] = history.newsletter_id
                fields |= {"outcome": "failed", "error_type": type(error).__name__}
                _log.info("orchestrator_run", extra=fields | {"duration_ms": _ms_since(started)})
                raise RunFailed("the orchestrator run did not complete") from error
            no_reply = reply.strip() == NO_REPLY
            fields["conversation_id"] = history.newsletter_id
            fields["outcome"] = "no_reply" if no_reply else "reply"
            _log.info("orchestrator_run", extra=fields | {"duration_ms": _ms_since(started)})
            return RunReply(
                text=None if no_reply else reply,
                newsletter=await self._shown(message, history),
            )

    async def _shown(self, message: ReviewerMessage, history: _History) -> Content | None:
        """The newsletter this message's run last presented or showed, including in an earlier
        attempt."""
        shown = history.shown.get(message.message_id)
        newsletter_id = history.newsletter_id
        if shown is None or newsletter_id is None:
            return None
        if shown.version is None:
            draft = await self._store.get_draft(newsletter_id)
            return draft.content if draft else None
        version = await self._store.get_version(newsletter_id, shown.version)
        return version.content if version else None

    async def _converse(
        self, message: ReviewerMessage, history: _History, on_progress: Progress
    ) -> str:
        await history.load(message)
        if history.unfinished() and history.owner is not None:
            owner = history.owner
            if owner == message.message_id:
                # A retried message is complete once its own run is.
                return await self._run(None, message, history, on_progress)
            await self._run(None, await history.recorded(owner), history, on_progress)
        return await self._run(user_prompt(message), message, history, on_progress)

    async def _run(
        self,
        prompt: str | None,
        message: ReviewerMessage,
        history: _History,
        on_progress: Progress,
    ) -> str:
        """Run the agent once for the message; with no prompt, resume its unfinished run."""
        # A resumed run starts from the step it resumes from, which is already saved.
        skip_step = prompt is None
        async with self._agent.iter(
            prompt,
            message_history=list(history.messages),
            deps=message,
            conversation_id=history.newsletter_id,
            usage_limits=UsageLimits(tool_calls_limit=self._max_tool_calls, request_limit=None),
        ) as run:
            async for node in run:
                if Agent.is_model_request_node(node) or Agent.is_call_tools_node(node):
                    step = (
                        node.request if Agent.is_model_request_node(node) else node.model_response
                    )
                    if not skip_step:
                        await history.add(message, step)
                    skip_step = False
                if Agent.is_call_tools_node(node):
                    await _call_tools(node, run, history.newsletter_id, on_progress)
        result = run.result
        if result is None or is_truncated_or_refused(result.response):
            raise UnexpectedModelBehavior("the final response was truncated or refused")
        return result.output


async def _call_tools(
    node: CallToolsNode[ReviewerMessage, str],
    run: AgentRun[ReviewerMessage, str],
    newsletter_id: str | None,
    on_progress: Progress,
) -> None:
    """Run the node's tool calls, sending a progress note and a log entry for each."""
    started: dict[str, float] = {}
    async with node.stream(run.ctx) as events:
        async for event in events:
            if isinstance(event, FunctionToolCallEvent):
                started[event.part.tool_call_id] = time.monotonic()
                await on_progress(progress_note(event.part.tool_name))
            elif isinstance(event, FunctionToolResultEvent):
                part = event.part
                _log.info(
                    "tool_call",
                    extra={
                        "conversation_id": newsletter_id,
                        "tool": part.tool_name,
                        "outcome": "returned" if isinstance(part, ToolReturnPart) else "retry",
                        "duration_ms": _ms_since(started.pop(part.tool_call_id)),
                    },
                )


def history_note(text: str) -> bytes:
    """A line from Pulse for the conversation history, as delivery's note of a send.

    Pydantic AI merges consecutive requests, so the next reviewer message joins it.
    """
    return ModelMessagesTypeAdapter.dump_json([ModelRequest(parts=[UserPromptPart(text)])])


def _superseded_results_replaced(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Replace each tool result from before the latest presented version with a placeholder.

    The store keeps every result in full; only the history the model sees is shortened.
    """
    presents = (n for n, message in enumerate(messages) if _results(message, [_PRESENT_DRAFT]))
    latest = max(presents, default=0)
    return [_placeholders(m) if n < latest else m for n, m in enumerate(messages)]


def _results(message: ModelMessage, tools: Sequence[str]) -> list[object]:
    """The successful results of the named tools in the message, in order."""
    if not isinstance(message, ModelRequest):
        return []
    return [
        part.content
        for part in message.parts
        # A refused tool returns its reason as a string, and did nothing.
        if isinstance(part, ToolReturnPart)
        and part.tool_name in tools
        and not isinstance(part.content, str)
    ]


def _placeholders(message: ModelMessage) -> ModelMessage:
    if not isinstance(message, ModelRequest):
        return message
    parts = [
        dataclasses.replace(part, content=f"Earlier {part.tool_name} result, superseded.")
        if isinstance(part, ToolReturnPart)
        else part
        for part in message.parts
    ]
    return dataclasses.replace(message, parts=parts)


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
