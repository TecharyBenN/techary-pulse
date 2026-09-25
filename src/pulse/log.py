"""Structured JSON logging to standard output."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else was passed through ``extra``.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Format each record as one JSON object per line.

    Fields passed with ``extra`` are included as top-level keys. Callers must pass
    IDs, counts, durations and error types only, never email content or agent output.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(
            (key, value) for key, value in vars(record).items() if key not in _STANDARD_ATTRS
        )
        if record.exc_info:
            entry["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Send all Pulse logging to standard output as JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
