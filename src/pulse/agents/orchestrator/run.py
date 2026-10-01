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
)

from pulse.agents.orchestrator.agent import NO_REPLY, user_prompt
from pulse.agents.orchestrator.tools import ShowResult, Tools, progress_note
from pulse.agents.runner import is_truncated_or_refused
from pulse.entities.base import Entity
from pulse.entities.content import Content
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import PulseError, RunFailed
from pulse.entities.store import Store

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
    """The open newsletter's conversation history, saving each step as it is added.

    With no open newsletter, the history starts empty and is held until a tool opens one; the
    exchange then becomes the new newsletter's history, and its message the first feedback.
    """

    def __init__(self, store: Store, message: ReviewerMessage, newsletter_id: str | None) -> None:
        self._store = store
        self._message = message
        self.newsletter_id = newsletter_id
        self.messages: list[ModelMessage] = []
        # The message whose run saved the latest step.
        self.owner: str | None = None
        # The latest newsletter each message's run presented or showed, by message ID.
        self.shown: dict[str, ShowResult] = {}

    async def load(self) -> None:
        """Record the message as feedback, then load the saved steps."""
        if self.newsletter_id is None:
            return
        await self._store.record_feedback(self.newsletter_id, self._message)
        rows = await self._store.load_history(self.newsletter_id)
        loaded = [
            (row.message_id, m)
            for row in rows
            for m in ModelMessagesTypeAdapter.validate_json(row.data)
        ]
        for message_id, message in loaded:
            self._note(message_id, message)
        self.messages = _superseded_results_replaced([message for _, message in loaded])
        self.owner = rows[-1].message_id if rows else None

    def unfinished(self) -> bool:
        """Whether the latest run stopped before its final response."""
        last = self.messages[-1] if self.messages else None
        return isinstance(last, ModelRequest) or (
            isinstance(last, ModelResponse) and bool(last.tool_calls)
        )

    async def add(self, message_id: str, message: ModelMessage) -> None:
        self.messages.append(message)
        self.owner = message_id
        self._note(message_id, message)
        if self.newsletter_id is not None:
            await self._save(self.newsletter_id, message_id, [message])
            return
        newsletter = await self._store.get_open_newsletter()
        if newsletter is not None:
            self.newsletter_id = newsletter.newsletter_id
            await self._store.record_feedback(self.newsletter_id, self._message)
            # Every step so far belongs to this message's run, because the history started empty.
            await self._save(self.newsletter_id, message_id, self.messages)

    def _note(self, message_id: str, message: ModelMessage) -> None:
        if shown := _results(message, _SHOWING):
            # During a run a result is the tool's own model; loaded from the store, it is a dict.
            self.shown[message_id] = ShowResult.model_validate(shown[-1], from_attributes=True)

    async def _save(
        self, newsletter_id: str, message_id: str, messages: Sequence[ModelMessage]
    ) -> None:
        for message in messages:
            data = ModelMessagesTypeAdapter.dump_json([message])
            await self._store.append_history(newsletter_id, message_id, data)


class Orchestrator:
    """Runs the orchestrator for each reviewer message, one run at a time, in arrival order."""

    def __init__(
        self,
        agent: Agent[None, str],
        store: Store,
        max_tool_calls: int,
        max_run_time: timedelta,
    ) -> None:
        self._agent = agent
        self._store = store
        self._max_tool_calls = max_tool_calls
        self._max_run_time = max_run_time
        # asyncio.Lock wakes its waiters first in, first out, which keeps arrival order.
        self._lock = asyncio.Lock()

    async def handle(
        self, message: ReviewerMessage, on_progress: Progress = _no_progress
    ) -> RunReply:
        """Raise RunFailed on failure."""
        async with self._lock:
            started = time.monotonic()
            newsletter = await self._store.get_open_newsletter()
            newsletter_id = newsletter.newsletter_id if newsletter else None
            fields: dict[str, object] = {
                "conversation_id": newsletter_id,
                "message_id": message.message_id,
                "channel": message.channel,
            }
            history = _History(self._store, message, newsletter_id)
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
        await history.load()
        if history.unfinished() and history.owner is not None:
            owner = history.owner
            reply = await self._run(None, owner, history, on_progress)
            # A retried message is complete once its own run is; a different message still runs.
            if owner == message.message_id:
                return reply
        return await self._run(user_prompt(message), message.message_id, history, on_progress)

    async def _run(
        self, prompt: str | None, message_id: str, history: _History, on_progress: Progress
    ) -> str:
        """Run the agent once; with no prompt, resume the unfinished run in the history."""
        # A resumed run starts from the step it resumes from, which is already saved.
        skip_step = prompt is None
        async with self._agent.iter(
            prompt,
            message_history=list(history.messages),
            conversation_id=history.newsletter_id,
            usage_limits=UsageLimits(tool_calls_limit=self._max_tool_calls, request_limit=None),
        ) as run:
            async for node in run:
                if Agent.is_model_request_node(node) or Agent.is_call_tools_node(node):
                    step = (
                        node.request if Agent.is_model_request_node(node) else node.model_response
                    )
                    if not skip_step:
                        await history.add(message_id, step)
                    skip_step = False
                if Agent.is_call_tools_node(node):
                    await _call_tools(node, run, history.newsletter_id, on_progress)
        result = run.result
        if result is None or is_truncated_or_refused(result.response):
            raise UnexpectedModelBehavior("the final response was truncated or refused")
        return result.output


async def _call_tools(
    node: CallToolsNode[None, str],
    run: AgentRun[None, str],
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
