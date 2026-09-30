"""Creates the adapters and connects them to the agents, services and entrypoints."""

import asyncio
import logging
import os
from collections.abc import Sequence
from datetime import timedelta

import pydantic_ai

from pulse.adapters.clock import SystemClock
from pulse.adapters.gateway import gateway_model
from pulse.adapters.store import SqliteStore
from pulse.adapters.tokens import JwtVerifier
from pulse.agents.orchestrator.agent import build_agent
from pulse.agents.orchestrator.run import Orchestrator
from pulse.config import Config, ConfigError, load_config
from pulse.entities.errors import PulseError
from pulse.entities.store import Store
from pulse.entrypoints.chat import create_app, serve
from pulse.entrypoints.cli import parse_args
from pulse.logging import configure_logging

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
    orchestrator = build_orchestrator(config, store, llm_key)
    clock = SystemClock()
    auth = config.auth
    verifier = JwtVerifier(auth.issuer, auth.audience, auth.jwks, clock)
    app = create_app(orchestrator, verifier, auth.reviewer_role, clock)
    await serve(app, config.chat.port)


def build_orchestrator(config: Config, store: Store, llm_key: str) -> Orchestrator:
    model = gateway_model(config.llm.base_url, llm_key, config.llm.models["orchestrator"])
    return Orchestrator(
        build_agent(model),
        store,
        config.orchestrator.max_tool_calls,
        timedelta(minutes=config.orchestrator.max_run_minutes),
    )


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment variable {name} is not set")
    return value
