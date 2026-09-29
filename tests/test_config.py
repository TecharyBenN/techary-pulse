from collections.abc import Callable
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from pulse.config import AGENT_NAMES, Config, ConfigError, load_config
from pulse.entities.lifecycle import OnApproval, Scheduled

EXAMPLES = Path(__file__).parent.parent / "config"


def _example() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((EXAMPLES / "config.example.yaml").read_text())
    return data


def _load(tmp_path: Path, data: dict[str, Any]) -> Config:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return load_config(path)


@pytest.mark.parametrize("name", ["config.example.yaml", "config.dev.example.yaml"])
def test_examples_load(name: str) -> None:
    config = load_config(EXAMPLES / name)

    assert set(config.llm.models) == set(AGENT_NAMES)


def test_values_are_parsed(tmp_path: Path) -> None:
    config = _load(tmp_path, _example())

    assert config.timezone == ZoneInfo("Europe/London")
    assert config.send == Scheduled(mode="scheduled", day="MON", time=time(9, 0))
    assert config.schedule.draft_cron == "30 17 * * FRI"


def test_draft_cron_can_be_unset(tmp_path: Path) -> None:
    data = _example()
    del data["schedule"]["draft_cron"]

    assert _load(tmp_path, data).schedule.draft_cron is None


def _set_reviewers(data: dict[str, Any]) -> None:
    data["reviewers"] = ["someone@example.com"]


def _set_operator_alerts(data: dict[str, Any]) -> None:
    data["operator_alerts"] = ["reviewer@techary.ai", "someone@example.com"]


def _set_all_staff(data: dict[str, Any]) -> None:
    data["all_staff"] = "all-staff@example.com"


def _set_subdomain(data: dict[str, Any]) -> None:
    data["reviewers"] = ["someone@mail.techary.ai"]


def _set_no_domain(data: dict[str, Any]) -> None:
    data["reviewers"] = ["techary.ai"]


@pytest.mark.parametrize(
    "change",
    [_set_reviewers, _set_operator_alerts, _set_all_staff, _set_subdomain, _set_no_domain],
)
def test_recipient_outside_allowed_domains_is_rejected(
    tmp_path: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    data = _example()
    change(data)

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


def test_recipient_domain_ignores_case(tmp_path: Path) -> None:
    data = _example()
    data["reviewers"] = ["Reviewer@Techary.AI"]
    data["allowed_recipient_domains"] = ["TECHARY.ai"]

    assert _load(tmp_path, data).reviewers == ["Reviewer@Techary.AI"]


def test_agent_without_model_is_rejected(tmp_path: Path) -> None:
    data = _example()
    del data["llm"]["models"]["judge"]

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


def test_model_naming_no_agent_is_rejected(tmp_path: Path) -> None:
    data = _example()
    data["llm"]["models"]["reviser"] = "gateway-mid-model"

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


@pytest.mark.parametrize("key", ["day", "time"])
def test_scheduled_send_needs_day_and_time(tmp_path: Path, key: str) -> None:
    data = _example()
    del data["send"][key]

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


def test_on_approval_send_needs_no_day_or_time(tmp_path: Path) -> None:
    data = _example()
    data["send"] = {"mode": "on_approval"}

    assert _load(tmp_path, data).send == OnApproval(mode="on_approval")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("day", "MONDAY"),
        ("day", "mon"),
        ("time", "9:00"),
        ("time", "24:00"),
        ("time", "09:60"),
        ("time", "09:00:00"),
        ("time", 540),
        ("mode", "weekly"),
    ],
)
def test_invalid_send_value_is_rejected(tmp_path: Path, key: str, value: object) -> None:
    data = _example()
    data["send"][key] = value

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


@pytest.mark.parametrize("zone", ["Europe/Nowhere", "../etc/passwd", ""])
def test_invalid_timezone_is_rejected(tmp_path: Path, zone: str) -> None:
    data = _example()
    data["timezone"] = zone

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    data = _example()
    data["edition"] = {"expire_after_days": 7}

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


def test_unknown_nested_key_is_rejected(tmp_path: Path) -> None:
    data = _example()
    data["chat"]["timeout"] = 5

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


@pytest.mark.parametrize(
    "path",
    [
        ("graph", "max_retries"),
        ("schedule", "poll_interval_minutes"),
        ("orchestrator", "max_tool_calls"),
        ("orchestrator", "max_run_minutes"),
        ("chat", "port"),
        ("chat", "max_attempts"),
        ("limits", "max_words"),
        ("retention_days",),
    ],
)
def test_counts_must_be_positive(tmp_path: Path, path: tuple[str, ...]) -> None:
    data = _example()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = 0

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


def test_duplicate_section_category_is_rejected(tmp_path: Path) -> None:
    data = _example()
    data["sections"].append(dict(data["sections"][0], title="Another title"))

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


def test_missing_key_is_rejected(tmp_path: Path) -> None:
    data = _example()
    del data["reviewers"]

    with pytest.raises(ConfigError):
        _load(tmp_path, data)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.yaml")


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("graph: [unclosed")

    with pytest.raises(ConfigError):
        load_config(path)


def test_config_is_frozen(tmp_path: Path) -> None:
    config = _load(tmp_path, _example())

    with pytest.raises(ValueError):
        config.retention_days = 1  # type: ignore[misc]
