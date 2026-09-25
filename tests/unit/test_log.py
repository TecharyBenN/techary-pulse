import json
import logging
import sys

from pulse.log import JsonFormatter


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("pulse", logging.INFO, __file__, 1, "run %s", ("started",), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formats_one_json_object_with_extra_fields() -> None:
    entry = json.loads(JsonFormatter().format(_record(run_id="2026-09-25", messages=3)))

    assert entry["level"] == "INFO"
    assert entry["logger"] == "pulse"
    assert entry["message"] == "run started"
    assert entry["run_id"] == "2026-09-25"
    assert entry["messages"] == 3
    assert entry["time"].endswith("+00:00")


def test_standard_record_attributes_are_not_repeated() -> None:
    entry = json.loads(JsonFormatter().format(_record()))

    assert set(entry) == {"time", "level", "logger", "message"}


def test_exceptions_carry_their_type() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "pulse", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )

    entry = json.loads(JsonFormatter().format(record))

    assert entry["error_type"] == "ValueError"
    assert "boom" in entry["traceback"]
