"""Runs one chat agent turn: the edition lock, history in and out."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior

from pulse.agents import CHAT
from pulse.agents.chat import ChatDeps, RunBuild, chat_agent
from pulse.agents.runner import AgentRunner
from pulse.config import Config, is_reviewer
from pulse.errors import ConversationError, PulseError
from pulse.mail import Mailbox
from pulse.models import OutgoingEmail, ReviewerMessage
from pulse.pipeline.runner import Clock
from pulse.store import EditionStore

log = logging.getLogger("pulse.conversation")


@dataclass(frozen=True)
class TurnResult:
    reply: str
    edition_id: str | None
    version_email: OutgoingEmail | None


Deliver = Callable[[TurnResult], Awaitable[None]]


def _prompt(message: ReviewerMessage) -> str:
    return (
        f"Channel: {message.channel}\nReviewer: {message.reviewer_name}\n\n"
        f"<reviewer_message>\n{message.text}\n</reviewer_message>"
    )


class ConversationRunner:
    """Runs the chat agent for the open edition's conversation, one turn at a time.

    At most one edition is open at a time, so one lock serialises every turn, from loading
    the history to saving the turn.
    """

    def __init__(
        self,
        config: Config,
        store: EditionStore,
        submissions: Mailbox,
        conversation: Mailbox,
        agents: AgentRunner,
        clock: Clock,
        run_build: RunBuild,
    ) -> None:
        self._config = config
        self._store = store
        self._submissions = submissions
        self._conversation = conversation
        self._agents = agents
        self._clock = clock
        self._run_build = run_build
        self._lock = asyncio.Lock()

    async def run_turn(
        self,
        message: ReviewerMessage,
        deliver: Deliver,
        handled_message_id: str | None = None,
    ) -> TurnResult:
        """Run the chat agent for ``message``, deliver the result, then save the turn.

        The turn's history and the message, as feedback, are saved against the edition only
        once ``deliver`` succeeds, with ``handled_message_id`` marked handled in the same
        transaction. A caller not in ``reviewers`` gets no agent run and nothing is delivered.

        Raises:
            ConversationError: If the gateway, a tool or ``deliver`` fails; nothing is saved.
        """
        if not is_reviewer(self._config, message.reviewer):
            return TurnResult(f"{message.reviewer} is not a reviewer.", None, None)

        async with self._lock:
            edition = await self._store.open_edition()
            edition_id = edition.id if edition is not None else None
            history = await self._store.load_history(edition_id) if edition_id else []
            deps = ChatDeps(
                caller=message.reviewer,
                reviewer_name=message.reviewer_name,
                message=message.text,
                channel=message.channel,
                edition_id=edition_id,
                store=self._store,
                submissions=self._submissions,
                conversation=self._conversation,
                config=self._config,
                agents=self._agents,
                clock=self._clock,
                run_build=self._run_build,
            )
            try:
                run = await chat_agent.run(
                    _prompt(message),
                    deps=deps,
                    message_history=history,
                    model=self._agents.model(CHAT),
                )
                result = TurnResult(
                    run.output, deps.new_edition_id or edition_id, deps.version_email
                )
                await deliver(result)
            except (ModelAPIError, UnexpectedModelBehavior, PulseError) as exc:
                raise ConversationError(str(exc)) from exc

            if result.edition_id is not None:
                await self._store.save_turn(
                    result.edition_id, run.new_messages(), message, handled_message_id
                )
            log.info(
                "chat turn run",
                extra={"conversation_id": result.edition_id, "channel": message.channel},
            )
            return result
