"""The pulse command: reads the configuration, builds every component and runs them."""

import argparse
import asyncio
import functools
import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pydantic_ai
from azure.identity.aio import CertificateCredential
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pythonjsonlogger.json import JsonFormatter

from pulse.adapters.graph import GraphMailbox, certificate_thumbprint, graph_client
from pulse.adapters.render import Renderer
from pulse.adapters.store import SqliteStore
from pulse.adapters.tokens import JwtVerifier
from pulse.agents.consolidator.agent import build_consolidator
from pulse.agents.extractor.agent import build_extractor
from pulse.agents.judge.agent import build_judge
from pulse.agents.orchestrator.agent import build_agent
from pulse.agents.orchestrator.run import Orchestrator, history_note
from pulse.agents.orchestrator.tools import Tools
from pulse.agents.sensitivity.agent import build_sensitivity
from pulse.agents.writer.agent import build_writer
from pulse.config import Config, ConfigError, load_config
from pulse.entities.errors import PulseError
from pulse.entities.mail import InboundEmail, Mailbox, ScreenedEmail, screen
from pulse.entities.store import Store
from pulse.entrypoints.chat import create_app, serve
from pulse.entrypoints.email import EmailChannel
from pulse.entrypoints.scheduler import poll
from pulse.services.delivery import Delivery
from pulse.services.operations import Operations
from pulse.services.outbox import Outbox

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
        _log.exception("start_failed")
        raise SystemExit(1) from error


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="path to config.yaml (default: ./config.yaml)",
    )
    parser = argparse.ArgumentParser(prog="pulse")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "serve", parents=[common], help="run the chat endpoint and delivery until stopped"
    )
    return parser.parse_args(argv)


async def _serve(config: Config) -> None:
    llm_key = _environment(config.llm.api_key_env)
    store = SqliteStore(config.state.db_path)
    await store.initialise()
    clock = functools.partial(datetime.now, UTC)
    renderer = Renderer(config.timezone)
    graph = config.graph
    _log.info(
        "graph_certificate", extra={"thumbprint": certificate_thumbprint(graph.certificate_path)}
    )
    credential = CertificateCredential(
        graph.tenant_id, graph.client_id, certificate_path=str(graph.certificate_path)
    )
    async with credential:
        client = graph_client(credential)
        submissions, conversation = (
            GraphMailbox(client, address, graph.max_retries)
            for address in (config.mailboxes.submissions, config.mailboxes.conversation)
        )
        # One run lock for orchestrator runs and delivery.
        lock = asyncio.Lock()
        outbox = build_outbox(config, conversation, store, renderer)
        operations = build_operations(config, store, submissions, outbox, renderer, clock)
        orchestrator = build_orchestrator(config, store, operations, llm_key, lock)
        delivery = build_delivery(
            config, store, submissions, conversation, outbox, renderer, clock, lock
        )
        channel = build_email_channel(
            config, store, conversation, orchestrator, operations, outbox, renderer
        )
        auth = config.auth
        verifier = JwtVerifier(auth.issuer, auth.audience, auth.jwks)
        app = create_app(
            orchestrator, verifier.verify, auth.reviewer_role, clock, renderer.markdown
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


def build_outbox(config: Config, conversation: Mailbox, store: Store, renderer: Renderer) -> Outbox:
    return Outbox(
        conversation,
        store,
        renderer.notice,
        reviewers=config.reviewers,
        operator_alerts=config.operator_alerts,
        subject_template=config.subject_template,
        timezone=config.timezone,
    )


def build_screen(config: Config) -> Callable[[InboundEmail], ScreenedEmail]:
    """The pre-filter, with the configured senders and sensitivity labels."""
    return functools.partial(
        screen,
        allowed_sender_domains=config.allowed_sender_domains,
        allowed_senders=config.allowed_senders,
        allowed_sensitivity_labels=config.allowed_sensitivity_labels,
    )


def build_operations(
    config: Config,
    store: Store,
    submissions: Mailbox,
    outbox: Outbox,
    renderer: Renderer,
    clock: Callable[[], datetime],
) -> Operations:
    return Operations(
        store,
        submissions,
        outbox,
        build_screen(config),
        renderer.reviewer_email,
        clock,
        send_rule=config.send,
        timezone=config.timezone,
        categories=list(config.categories),
        max_words=config.limits.max_words,
    )


def build_orchestrator(
    config: Config, store: Store, operations: Operations, llm_key: str, lock: asyncio.Lock
) -> Orchestrator:
    provider = OpenAIProvider(base_url=config.llm.base_url, api_key=llm_key)

    def model(agent: str) -> Model:
        return OpenAIChatModel(config.llm.models[agent], provider=provider)

    categories = config.categories
    tools = Tools(
        operations,
        store,
        build_extractor(model("extractor"), categories),
        build_sensitivity(model("sensitivity")),
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
    renderer: Renderer,
    clock: Callable[[], datetime],
    lock: asyncio.Lock,
) -> Delivery:
    mailboxes = config.mailboxes
    return Delivery(
        store,
        conversation,
        submissions,
        outbox,
        renderer.newsletter,
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
    renderer: Renderer,
) -> EmailChannel:
    mailboxes = config.mailboxes
    return EmailChannel(
        conversation,
        orchestrator,
        store,
        outbox,
        operations.reviewer_email,
        renderer.newsletter,
        renderer.reply,
        max_attempts=config.chat.max_attempts,
        processed_folder=mailboxes.processed_folder,
        rejected_folder=mailboxes.rejected_folder,
    )


def configure_logging() -> None:
    """JSON objects, one per line, on standard output."""
    handler = logging.StreamHandler(sys.stdout)
    fields = {"levelname": "level", "message": "event"}
    formatter = JsonFormatter(
        "{levelname}{message}", style="{", rename_fields=fields, timestamp="time"
    )
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment variable {name} is not set")
    return value
