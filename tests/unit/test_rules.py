from pathlib import Path

import pytest

from pulse.config import Config, load_config
from pulse.models import Draft, ExtractRecord
from pulse.pipeline.rules import (
    exclude,
    exclusion_reason,
    prefilter,
    rejection_reason,
    sections_in_order,
)

from ..support import message

LABEL = "3a1b7f0e-5c2d-4e8a-9b6f-1d2c3e4f5a6b"
OTHER = "0f0f0f0f-0000-4000-8000-000000000000"
ENABLED = "MSIP_Label_{}_Enabled=true"


@pytest.fixture
def config(config_dir: Path) -> Config:
    base = load_config(config_dir / "config.example.yaml")
    return base.model_copy(update={"allowed_sensitivity_labels": [LABEL]})


def test_genuine_update_passes(config: Config) -> None:
    assert rejection_reason(message(), config) is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"sender_address": "someone@example.com"}, "sender domain is not allowed"),
        ({"sender_address": "someone@TECHARY.AI.example.com"}, "sender domain is not allowed"),
        ({"sender_address": "not-an-address"}, "sender address is not valid"),
        ({"headers": {"msip_labels": f"MSIP_Label_{OTHER}_Enabled=true"}}, "label is not allowed"),
        (
            {"headers": {"msip_labels": f"{ENABLED.format(LABEL)}; {ENABLED.format(OTHER)}"}},
            "label is not allowed",
        ),
        ({"headers": {"auto-submitted": "auto-replied"}}, "automatic reply"),
        ({"headers": {"x-auto-response-suppress": "All"}}, "automatic reply"),
        ({"body": "   ok   "}, "body is too short"),
    ],
)
def test_rejections(config: Config, overrides: dict[str, object], reason: str) -> None:
    result = rejection_reason(message(**overrides), config)  # type: ignore[arg-type]
    assert result is not None and reason in result


def test_sender_domain_matches_ignoring_case(config: Config) -> None:
    assert rejection_reason(message(sender_address="Priya.Shah@Techary.AI"), config) is None


def test_allowed_label_and_unlabelled_pass(config: Config) -> None:
    labelled = message(headers={"msip_labels": f"MSIP_Label_{LABEL.upper()}_Enabled=true"})
    assert rejection_reason(labelled, config) is None
    assert rejection_reason(message(), config) is None


def test_auto_submitted_no_passes(config: Config) -> None:
    assert rejection_reason(message(headers={"auto-submitted": "no"}), config) is None


def test_allowed_senders_limits_contributors_when_set(config: Config) -> None:
    limited = config.model_copy(update={"allowed_senders": ["tom.evans@techary.ai"]})
    assert rejection_reason(message(), limited) == "sender is not allowed"
    assert rejection_reason(message(sender_address="Tom.Evans@techary.ai"), limited) is None


def test_prefilter_splits_messages(config: Config) -> None:
    passed, rejected = prefilter(
        [message("a"), message("b", sender_address="x@example.com")], config
    )
    assert [m.id for m in passed] == ["a"]
    assert [r.message.id for r in rejected] == ["b"]


def record(**overrides: object) -> ExtractRecord:
    base: dict[str, object] = {
        "message_id": "m1",
        "is_update": True,
        "exclusion_reason": None,
        "category": "customer_win",
        "summary": "s",
        "facts": [],
        "people": [],
        "sensitivity": [],
    }
    return ExtractRecord.model_validate(base | overrides)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, None),
        (
            {"is_update": False, "exclusion_reason": "not_an_update", "category": None},
            "not_an_update",
        ),
        ({"is_update": False}, "not_an_update"),
        ({"exclusion_reason": "unclear", "category": None}, "unclear"),
        ({"category": None}, "no_matching_section"),
        ({"sensitivity": [{"type": "commercial", "evidence": "deal value"}]}, "sensitivity"),
    ],
)
def test_exclusion_reason(overrides: dict[str, object], reason: str | None) -> None:
    assert exclusion_reason(record(**overrides)) == reason


def test_exclude_splits_records() -> None:
    included, excluded = exclude([record(), record(message_id="m2", category=None)])
    assert [r.message_id for r in included] == ["m1"]
    assert [(e.record.message_id, e.reason) for e in excluded] == [("m2", "no_matching_section")]


def test_sections_follow_config_order_and_omit_empty(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    entry = {"item_id": "i", "text": "t", "people": []}
    draft = Draft.model_validate(
        {
            "intro": "i",
            "sections": [
                {"category": "shout_out", "entries": [entry]},
                {"category": "team_news", "entries": []},
                {"category": "customer_win", "entries": [entry]},
            ],
        }
    )
    assert [s.category for s, _ in sections_in_order(draft, config)] == [
        "customer_win",
        "shout_out",
    ]
