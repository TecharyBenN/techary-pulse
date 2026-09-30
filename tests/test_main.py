from pathlib import Path

import pytest

from pulse.main import main

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
