"""Creates the adapters and connects them to the agents, services and entrypoints."""

import argparse


def main() -> None:
    # Commands arrive from phase 2 onwards.
    argparse.ArgumentParser(prog="pulse").parse_args()
