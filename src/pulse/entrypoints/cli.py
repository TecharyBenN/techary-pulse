"""The command line."""

import argparse
from collections.abc import Sequence
from pathlib import Path


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
