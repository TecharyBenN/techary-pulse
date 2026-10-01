"""Creates the adapters and connects them to the agents, services and entrypoints."""

import asyncio
import functools
import logging
import os
from collections.abc import Sequence
from datetime import timedelta

import httpx
import pydantic_ai
from pydantic_ai.models import Model

from pulse.adapters.clock import SystemClock
from pulse.adapters.gateway import gateway_model
from pulse.adapters.graph import GRAPH_URL, CertificateCredential, GraphMailbox
from pulse.adapters.render import Renderer
from pulse.adapters.store import SqliteStore
from pulse.adapters.tokens import JwtVerifier
from pulse.agents.consolidator.agent import build_consolidator
from pulse.agents.extractor.agent import build_extractor
from pulse.agents.judge.agent import build_judge
from pulse.agents.orchestrator.agent import build_agent
from pulse.agents.orchestrator.run import Orchestrator, history_note
from pulse.agents.orchestrator.tools import Tools
from pulse.agents.writer.agent import build_writer
from pulse.config import Config, ConfigError, load_config
from pulse.entities.clock import Clock
from pulse.entities.errors import PulseError
from pulse.entities.mail import Mailbox, screen
from pulse.entities.store import Store
from pulse.entrypoints.chat import create_app, serve
from pulse.entrypoints.cli import parse_args
from pulse.entrypoints.email import EmailChannel
from pulse.entrypoints.scheduler import poll
from pulse.logging import configure_logging
from pulse.services.delivery import Delivery
from pulse.services.mail import Outbox
from pulse.services.operations import Operations

_log = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging()
    # Standard output carries JSON logs only.
    pydantic_ai.BANNER_ENABLED = False
    try:
        config = load_config(args.config)
        asyncio.run(_serve(config))
    except PulseError as error:
        _log.error("start_failed", extra={"error_type": type(error).__name__, "detail": str(error)})
        raise SystemExit(1) from error


async def _serve(config: Config) -> None:
    llm_key = _environment(config.llm.api_key_env)
    store = SqliteStore(config.state.db_path)
    await store.initialise()
    clock = SystemClock()
    graph = config.graph
    credential = CertificateCredential(graph.tenant_id, graph.client_id, graph.certificate_path)
    _log.info("graph_certificate", extra={"thumbprint": credential.thumbprint})
    async with httpx.AsyncClient(base_url=GRAPH_URL) as client:
        submissions, conversation = (
            GraphMailbox(client, credential.token, address, graph.max_retries)
            for address in (config.mailboxes.submissions, config.mailboxes.conversation)
        )
        # One run lock for orchestrator runs and delivery.
        lock = asyncio.Lock()
        outbox = build_outbox(config, conversation, store)
        operations = build_operations(config, store, submissions, outbox, clock)
        orchestrator = build_orchestrator(config, store, operations, llm_key, lock)
        delivery = build_delivery(config, store, submissions, conversation, outbox, clock, lock)
        channel = build_email_channel(config, store, conversation, orchestrator, operations, outbox)
        auth = config.auth
        verifier = JwtVerifier(auth.issuer, auth.audience, auth.jwks, clock)
        app = create_app(
            orchestrator, verifier, auth.reviewer_role, clock, _renderer(config).markdown
        )
        stop = asyncio.Event()
        interval = timedelta(seconds=config.schedule.poll_interval_seconds)
        # Email first, so a withdrawal by email takes effect before a send due in the same poll.
        jobs = {"email": channel.poll, "delivery": delivery.deliver}
        polling = asyncio.create_task(poll(jobs, interval, stop))
        try:
            await serve(app, config.chat.port)
        finally:
            # A poll job in progress finishes before Pulse exits.
            stop.set()
            await polling


def build_outbox(config: Config, conversation: Mailbox, store: Store) -> Outbox:
    return Outbox(
        conversation,
        store,
        _renderer(config).notice,
        reviewers=config.reviewers,
        operator_alerts=config.operator_alerts,
        subject_template=config.subject_template,
        timezone=config.timezone,
    )


def build_operations(
    config: Config, store: Store, submissions: Mailbox, outbox: Outbox, clock: Clock
) -> Operations:
    screen_email = functools.partial(
        screen,
        allowed_sender_domains=config.allowed_sender_domains,
        allowed_senders=config.allowed_senders,
        allowed_sensitivity_labels=config.allowed_sensitivity_labels,
    )
    return Operations(
        store,
        submissions,
        outbox,
        screen_email,
        _renderer(config).reviewer_email,
        clock,
        send_rule=config.send,
        timezone=config.timezone,
        categories=list(config.categories),
        max_words=config.limits.max_words,
    )


def build_orchestrator(
    config: Config, store: Store, operations: Operations, llm_key: str, lock: asyncio.Lock
) -> Orchestrator:
    def model(agent: str) -> Model:
        return gateway_model(config.llm.base_url, llm_key, config.llm.models[agent])

    categories = config.categories
    tools = Tools(
        operations,
        store,
        build_extractor(model("extractor"), categories),
        build_consolidator(model("consolidator"), categories),
        build_writer(
            model("writer"),
            categories,
            {section.category: section.title for section in config.sections},
            config.headline_title,
            config.limits.max_words,
        ),
        build_judge(model("judge")),
        categories,
    )
    return Orchestrator(
        build_agent(model("orchestrator"), tools.toolset()),
        store,
        config.orchestrator.max_tool_calls,
        timedelta(minutes=config.orchestrator.max_run_minutes),
        lock,
    )


def build_delivery(
    config: Config,
    store: Store,
    submissions: Mailbox,
    conversation: Mailbox,
    outbox: Outbox,
    clock: Clock,
    lock: asyncio.Lock,
) -> Delivery:
    mailboxes = config.mailboxes
    return Delivery(
        store,
        conversation,
        submissions,
        outbox,
        _renderer(config).newsletter,
        history_note,
        clock,
        lock,
        all_staff=config.all_staff,
        reply_to=mailboxes.submissions,
        subject_template=config.subject_template,
        timezone=config.timezone,
        processed_folder=mailboxes.processed_folder,
        rejected_folder=mailboxes.rejected_folder,
    )


def build_email_channel(
    config: Config,
    store: Store,
    conversation: Mailbox,
    orchestrator: Orchestrator,
    operations: Operations,
    outbox: Outbox,
) -> EmailChannel:
    renderer = _renderer(config)
    mailboxes = config.mailboxes
    return EmailChannel(
        conversation,
        orchestrator,
        store,
        outbox,
        operations.review,
        renderer.reviewer_email,
        renderer.newsletter,
        renderer.reply,
        max_attempts=config.chat.max_attempts,
        processed_folder=mailboxes.processed_folder,
        rejected_folder=mailboxes.rejected_folder,
    )


def _renderer(config: Config) -> Renderer:
    return Renderer(config.timezone)


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment variable {name} is not set")
    return value
