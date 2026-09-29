from pathlib import Path

import pytest

from pulse.main import main

EXAMPLE = Path(__file__).parent.parent / "config" / "config.dev.example.yaml"


@pytest.mark.parametrize("missing", ["PULSE_LLM_API_KEY", "PULSE_CHAT_GATEWAY_KEY"])
def test_serve_stops_when_a_key_is_not_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    monkeypatch.setenv("PULSE_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("PULSE_CHAT_GATEWAY_KEY", "gateway-key")
    monkeypatch.delenv(missing)

    with pytest.raises(SystemExit) as stopped:
        main(["serve", "--config", str(EXAMPLE)])

    assert stopped.value.code == 1
    assert missing in capsys.readouterr().out


def test_serve_stops_when_the_config_cannot_be_loaded(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["serve", "--config", str(tmp_path / "missing.yaml")])
