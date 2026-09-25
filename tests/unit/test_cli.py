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


@pytest.mark.parametrize("command", [["run"], ["run", "--dry-run"], ["schedule"]])
def test_commands_load_config_then_report_not_implemented(
    command: list[str], config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([*command, "--config", str(config_dir / "config.example.yaml")])

    assert code == EXIT_FAILED
    messages = [json.loads(line)["message"] for line in capsys.readouterr().out.splitlines()]
    assert messages == ["configuration loaded", "command not implemented yet"]
