"""Command-line interface: ``pulse run`` and ``pulse schedule``."""

import argparse
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pulse.agents.runner import AgentRunner
from pulse.config import Config, load_config
from pulse.errors import AgentResponseError, ConfigError, PulseError
from pulse.log import configure_logging
from pulse.mail import GraphMailbox, msal_token
from pulse.pipeline.runner import run_pipeline

log = logging.getLogger("pulse")

EXIT_OK = 0
EXIT_FAILED = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse", description="Draft the weekly Pulse newsletter from staff updates."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the pipeline once and exit")
    run.add_argument(
        "--dry-run", action="store_true", help="run every step without sending or moving mail"
    )
    schedule = commands.add_parser("schedule", help="run the pipeline on the configured schedule")

    for command in (run, schedule):
        command.add_argument(
            "--config",
            type=Path,
            default=Path("config.yaml"),
            help="path to config.yaml (default: ./config.yaml)",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, validate configuration and run the requested command."""
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("configuration invalid", extra={"error": str(exc)})
        return EXIT_FAILED
    log.info("configuration loaded", extra={"command": args.command})
    if args.command == "run":
        return _run(config, dry_run=args.dry_run)
    # The scheduler arrives in phase 4.
    log.error("command not implemented yet", extra={"command": args.command})
    return EXIT_FAILED


def _run(config: Config, dry_run: bool) -> int:
    try:
        mailbox = GraphMailbox(config.graph, config.mailbox, token=msal_token(config.graph))
        result = run_pipeline(
            config, mailbox, AgentRunner(config.llm), lambda: datetime.now(UTC), dry_run=dry_run
        )
    except AgentResponseError as exc:
        # The detail can quote model output, which never goes in the logs.
        log.error(
            "run failed",
            extra={"error_type": type(exc).__name__, "agent": exc.agent, "subject": exc.subject},
        )
        return EXIT_FAILED
    except (PulseError, OSError) as exc:
        log.error("run failed", extra={"error_type": type(exc).__name__, "error": str(exc)})
        return EXIT_FAILED
    log.info("run finished", extra={"status": result.status, "run_id": result.manifest.run_id})
    return EXIT_OK
