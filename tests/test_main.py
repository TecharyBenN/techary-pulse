import io
import json
import logging
from pathlib import Path

import pytest

from pulse.main import configure_logging, main

EXAMPLE = Path(__file__).parent.parent / "config" / "config.dev.example.yaml"


def test_serve_stops_when_the_llm_key_is_not_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PULSE_LLM_API_KEY", raising=False)

    with pytest.raises(SystemExit) as stopped:
        main(["serve", "--config", str(EXAMPLE)])

    assert stopped.value.code == 1
    assert "PULSE_LLM_API_KEY" in capsys.readouterr().out


def test_serve_stops_when_the_config_cannot_be_loaded(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["serve", "--config", str(tmp_path / "missing.yaml")])


def _logged(monkeypatch: pytest.MonkeyPatch, log: object) -> dict[str, object]:
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    configure_logging()
    assert callable(log)
    log(logging.getLogger("pulse.test"))
    [line] = stdout.getvalue().splitlines()
    entry: dict[str, object] = json.loads(line)
    return entry


def test_log_entry_is_one_json_line_with_its_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _logged(monkeypatch, lambda log: log.info("started", extra={"count": 1}))

    assert (entry["event"], entry["level"], entry["count"]) == ("started", "INFO", 1)
    assert str(entry["time"]).endswith("+00:00")


def test_exception_is_logged_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(log: logging.Logger) -> None:
        try:
            raise ValueError("the reason")
        except ValueError:
            log.exception("failed")

    entry = _logged(monkeypatch, failed)

    assert "ValueError: the reason" in str(entry["exc_info"])
