"""Command-line interface: ``pulse run`` and ``pulse schedule``."""

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from pulse.config import load_config
from pulse.errors import ConfigError
from pulse.log import configure_logging

log = logging.getLogger("pulse")

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
        load_config(args.config)
    except ConfigError as exc:
        log.error("configuration invalid", extra={"error": str(exc)})
        return EXIT_FAILED
    log.info("configuration loaded", extra={"command": args.command})
    # The pipeline arrives in phase 1 and the scheduler in phase 4.
    log.error("command not implemented yet", extra={"command": args.command})
    return EXIT_FAILED
