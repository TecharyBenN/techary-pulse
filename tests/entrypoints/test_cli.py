from pathlib import Path

import pytest

from pulse.main import parse_args


def test_serve_reads_config_yaml_by_default() -> None:
    args = parse_args(["serve"])

    assert (args.command, args.config) == ("serve", Path("config.yaml"))


def test_serve_takes_a_config_path() -> None:
    assert parse_args(["serve", "--config", "/config/config.yaml"]).config == Path(
        "/config/config.yaml"
    )


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_serve_runs_the_chat_endpoint_and_delivery(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--help"])

    assert "run the chat endpoint and delivery until stopped" in capsys.readouterr().out
