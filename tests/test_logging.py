import io
import json
import logging

import pytest

from pulse.logging import JsonFormatter, configure_logging


def _record(**extra: object) -> logging.LogRecord:
    return logging.makeLogRecord({"msg": "run_started", "levelname": "INFO", **extra})


def test_record_is_one_json_line_with_required_fields() -> None:
    line = JsonFormatter().format(_record())

    assert "\n" not in line
    entry = json.loads(line)
    assert entry["event"] == "run_started"
    assert entry["level"] == "INFO"
    assert entry["time"].endswith("+00:00")


def test_extra_fields_are_included() -> None:
    entry = json.loads(JsonFormatter().format(_record(conversation_id="n-1", count=3)))

    assert entry["conversation_id"] == "n-1"
    assert entry["count"] == 3


def test_exception_gives_type_only() -> None:
    error = ValueError("secret message text")
    record = _record(levelname="ERROR", exc_info=(ValueError, error, None))

    line = JsonFormatter().format(record)

    assert json.loads(line)["error_type"] == "ValueError"
    assert "secret message text" not in line
    assert "Traceback" not in line


def test_configure_logging_writes_json_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)

    configure_logging()
    logging.getLogger("pulse.test").info("started", extra={"count": 1})

    assert json.loads(stdout.getvalue())["event"] == "started"
