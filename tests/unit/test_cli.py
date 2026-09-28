import json
from pathlib import Path

import pytest

from pulse.cli import EXIT_FAILED, main


def test_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "run" in capsys.readouterr().out


def test_invalid_config_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["run", "--config", str(tmp_path / "missing.yaml")])

    assert code == EXIT_FAILED
    entry = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert entry["message"] == "configuration invalid"


def test_schedule_reports_not_implemented(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["schedule", "--config", str(config_dir / "config.example.yaml")])

    assert code == EXIT_FAILED
    messages = [json.loads(line)["message"] for line in capsys.readouterr().out.splitlines()]
    assert messages == ["configuration loaded", "command not implemented yet"]


def test_run_without_the_certificate_fails_cleanly(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", "--dry-run", "--config", str(config_dir / "config.example.yaml")])

    assert code == EXIT_FAILED
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last["message"] == "run failed" and last["error_type"] == "FileNotFoundError"
