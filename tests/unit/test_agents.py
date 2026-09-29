from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pulse.agents import Consolidator, Drafter, Extractor, Judge, Reviser
from pulse.agents.runner import AgentRunner
from pulse.config import Config, load_config
from pulse.errors import AgentResponseError, GatewayError
from pulse.models import (
    CleanedEmail,
    ExcludedForReviser,
    ExtractRecord,
    ItemWithSenders,
    ReviseInput,
)

from ..support import ITEM_ROWS, SOURCE_ROWS, scripted

pytestmark = pytest.mark.anyio

EMAIL = CleanedEmail(
    message_id="m1",
    sender_name="Priya Shah",
    sender_address="priya.shah@techary.ai",
    subject="Signed Northwind Retail",
    received_at=datetime(2026, 9, 22, 9, tzinfo=UTC),
    body="Tom Evans and I signed Northwind Retail.",
)


def record(message_id: str = "m1", category: str | None = "customer_win") -> dict[str, object]:
    return {
        "message_id": message_id,
        "is_update": True,
        "exclusion_reason": None,
        "category": category,
        "summary": "A new customer.",
        "facts": ["Signed Northwind Retail"],
        "people": ["Priya Shah", "Tom Evans"],
        "sensitivity": [],
    }


@pytest.fixture
def config(config_dir: Path) -> Config:
    return load_config(config_dir / "config.example.yaml")


def runner(config: Config, **models: FunctionModel) -> AgentRunner:
    return AgentRunner(config.llm, models=models)


def test_every_agent_has_no_tools() -> None:
    assert all(agent.tools == () for agent in (Extractor, Consolidator, Drafter, Judge, Reviser))


async def test_valid_response_is_returned(config: Config) -> None:
    result = await runner(config, extractor=scripted([record()])).run(
        Extractor(config.sections), EMAIL
    )
    assert result.category == "customer_win"


async def test_invalid_response_is_retried_once(config: Config) -> None:
    model = scripted(["not json", record()])
    result = await runner(config, extractor=model).run(Extractor(config.sections), EMAIL)
    assert result.message_id == "m1"


async def test_second_invalid_response_names_agent_message_and_error(config: Config) -> None:
    model = scripted([record(message_id="other"), record(message_id="other")])
    with pytest.raises(AgentResponseError) as error:
        await runner(config, extractor=model).run(Extractor(config.sections), EMAIL)
    assert error.value.agent == "extractor"
    assert error.value.subject == "message m1"
    assert "message_id must be m1" in error.value.detail


async def test_unconfigured_category_is_invalid(config: Config) -> None:
    model = scripted([record(category="birthdays")] * 2)
    with pytest.raises(AgentResponseError, match="birthdays is not configured"):
        await runner(config, extractor=model).run(Extractor(config.sections), EMAIL)


async def test_gateway_error_is_raised_as_gateway_error(config: Config) -> None:
    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m")

    with pytest.raises(GatewayError):
        await runner(config, extractor=FunctionModel(fail)).run(Extractor(config.sections), EMAIL)


def test_extractor_instructions_include_configured_definitions(config: Config) -> None:
    instructions = Extractor(config.sections).instructions()
    for section in config.sections:
        assert f"- {section.category}: {section.definition}" in instructions


def item(item_id: str, sources: list[str], category: str = "customer_win") -> dict[str, object]:
    return {
        "item_id": item_id,
        "category": category,
        "facts": ["f"],
        "people": [],
        "source_message_ids": sources,
    }


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([item("i1", ["m1", "m2", "m9"])], "unknown source_message_ids: m9"),
        ([item("i1", ["m1"])], "records missing from every item: m2"),
        ([item("i1", ["m1", "m2"]), item("i2", ["m2"])], "records in more than one item: m2"),
        ([item("i1", ["m1", "m2"], category="birthdays")], "birthdays is not configured"),
    ],
)
async def test_consolidator_checks(config: Config, items: list[object], message: str) -> None:
    records = [ExtractRecord.model_validate(record(i)) for i in ("m1", "m2")]
    model = scripted([{"headline": "h", "items": items}] * 2)
    with pytest.raises(AgentResponseError, match=message):
        await runner(config, consolidator=model).run(Consolidator(config.sections), records)


def test_config_requires_a_model_for_each_agent(config_dir: Path, tmp_path: Path) -> None:
    text = (config_dir / "config.example.yaml").read_text().replace("    judge:", "    jduge:")
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(Exception, match=r"missing \['judge'\], unknown \['jduge'\]"):
        load_config(path)


# Reviser: the seeded edition's item, and its excluded record offered for restoring.

ITEM = ItemWithSenders.model_validate(ITEM_ROWS[0].record)
_RECORD, _REASON = ITEM_ROWS[1].excluded()
EXCLUDED = ExcludedForReviser(
    item_id=ITEM_ROWS[1].item_id,
    record=_RECORD,
    reason=_REASON,
    sender_names=[str(SOURCE_ROWS[1].sender_name)],
)


def revise_input(**overrides: Any) -> ReviseInput:
    kwargs: dict[str, Any] = dict(
        draft={
            "intro": "A good week.",
            "sections": [
                {
                    "category": "customer_win",
                    "entries": [
                        {
                            "item_id": "item-1",
                            "text": "Priya Shah signed Northwind Retail.",
                            "people": ["Priya Shah"],
                        }
                    ],
                }
            ],
        },
        headline="A strong week",
        items=[ITEM],
        excluded=[EXCLUDED],
        feedback=["Please also mention Ben's promotion"],
        instruction="Add Ben's promotion",
        reviewer_message="Can you add Ben's promotion too?",
        failures=[],
    )
    kwargs.update(overrides)
    return ReviseInput.model_validate(kwargs)


def revision(item_ids: list[str], **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = dict(
        headline="A strong week",
        draft={
            "intro": "A good week.",
            "sections": [
                {
                    "category": "customer_win",
                    "entries": [
                        {
                            "item_id": "item-1",
                            "text": "Priya Shah signed Northwind Retail.",
                            "people": ["Priya Shah"],
                        }
                    ],
                },
                {
                    "category": "team_news",
                    "entries": [
                        {
                            "item_id": "excluded-1",
                            "text": "Ben Carter was promoted.",
                            "people": ["Ben Carter"],
                        }
                    ],
                },
            ],
        },
        item_ids=item_ids,
        changes=["Restored Ben's promotion"],
        not_applied=[],
    )
    result.update(overrides)
    return result


async def test_valid_response_restoring_an_excluded_record_is_accepted(config: Config) -> None:
    result = await runner(config, reviser=scripted([revision(["item-1", "excluded-1"])])).run(
        Reviser(config.sections), revise_input()
    )
    assert result.item_ids == ["item-1", "excluded-1"]
    assert result.changes == ["Restored Ben's promotion"]


async def test_unknown_item_id_is_invalid(config: Config) -> None:
    model = scripted([revision(["item-1", "unknown-item"])] * 2)
    with pytest.raises(AgentResponseError, match="unknown item_ids: unknown-item"):
        await runner(config, reviser=model).run(Reviser(config.sections), revise_input())


async def test_entry_referencing_an_item_not_in_item_ids_is_invalid(config: Config) -> None:
    # The draft's "excluded-1" entry is present, but item_ids omits it.
    bad = revision(["item-1"])
    model = scripted([bad] * 2)
    with pytest.raises(
        AgentResponseError, match="entries reference items not in item_ids: excluded-1"
    ):
        await runner(config, reviser=model).run(Reviser(config.sections), revise_input())


async def test_second_invalid_response_names_the_reviser_agent(config: Config) -> None:
    model = scripted([revision(["item-1", "unknown"]), revision(["item-1", "unknown"])])
    with pytest.raises(AgentResponseError) as error:
        await runner(config, reviser=model).run(Reviser(config.sections), revise_input())
    assert error.value.agent == "reviser"
