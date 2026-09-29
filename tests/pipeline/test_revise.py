import json
from pathlib import Path
from typing import Any

import pytest

from pulse.agents.runner import AgentRunner
from pulse.config import Config, load_config
from pulse.errors import AgentResponseError
from pulse.models import Feedback
from pulse.pipeline.runner import run_revision
from pulse.store import EditionStore

from ..support import DRAFT_V1, JUDGE_PASS, REVIEWER, answering, at, scripted, seed_edition

pytestmark = pytest.mark.anyio

DRAFT_JSON = DRAFT_V1.model_dump(mode="json")


def revision_of(item_ids: list[str], **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = dict(
        headline="A strong week",
        draft=DRAFT_JSON
        if item_ids == ["item-1"]
        else {
            "intro": "A good week, with a promotion to celebrate.",
            "sections": [
                *DRAFT_JSON["sections"],
                {
                    "category": "team_news",
                    "entries": [
                        {
                            "item_id": "excluded-1",
                            "text": "Ben Carter was promoted to senior service desk analyst.",
                            "people": ["Ben Carter"],
                        }
                    ],
                },
            ],
        },
        item_ids=item_ids,
        changes=["Added Ben's promotion"],
        not_applied=[],
    )
    result.update(overrides)
    return result


@pytest.fixture
def config(config_dir: Path) -> Config:
    return load_config(config_dir / "config.example.yaml")


@pytest.fixture
def store(tmp_path: Path) -> EditionStore:
    return EditionStore(tmp_path / "state" / "pulse.db")


async def make_edition(store: EditionStore) -> str:
    edition_id = await seed_edition(store)
    await store.save_turn(
        edition_id,
        [],
        Feedback(
            reviewer=REVIEWER,
            channel="email",
            text="Can you add Ben's promotion too?",
            received_at=at(23),
        ),
    )
    return edition_id


def runner(config: Config, **models: Any) -> AgentRunner:
    return AgentRunner(config.llm, models=models)


async def test_run_revision_returns_a_new_version_with_the_reviser_fields(
    config: Config, store: EditionStore
) -> None:
    edition_id = await make_edition(store)
    agents = runner(
        config,
        reviser=scripted([revision_of(["item-1", "excluded-1"])]),
        judge=scripted([JUDGE_PASS]),
    )

    version, failures = await run_revision(
        config,
        store,
        agents,
        edition_id,
        new_version_number=2,
        instruction="Add Ben's promotion",
        reviewer_message="Can you add Ben's promotion too?",
        creator=REVIEWER,
        now=at(24),
    )

    assert failures == []
    assert version.number == 2
    assert version.headline == "A strong week"
    assert version.item_ids == ["item-1", "excluded-1"]
    assert version.changes == ["Added Ben's promotion"]
    assert version.not_applied == []
    assert version.creator == REVIEWER
    assert version.created_at == at(24)


async def test_a_restored_records_category_comes_from_where_the_draft_places_it(
    config: Config, store: EditionStore
) -> None:
    edition_id = await make_edition(store)
    agents = runner(
        config,
        reviser=scripted([revision_of(["item-1", "excluded-1"])]),
        judge=scripted([JUDGE_PASS]),
    )
    version, _ = await run_revision(
        config,
        store,
        agents,
        edition_id,
        new_version_number=2,
        instruction="Add Ben's promotion",
        reviewer_message="Can you add Ben's promotion too?",
        creator=REVIEWER,
        now=at(24),
    )
    edition = await store.open_edition()
    assert edition is not None
    await store.add_version(edition.model_copy(update={"current_version": 2}), version)

    # A second revision reads the restored record back from the saved draft, so it must be
    # offered to the reviser as an item, in the "team_news" category, not as excluded again.
    prompts: list[str] = []

    def capture(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return revision_of(["item-1", "excluded-1"], changes=[])

    agents2 = runner(config, reviser=answering(capture), judge=scripted([JUDGE_PASS]))
    await run_revision(
        config,
        store,
        agents2,
        edition_id,
        new_version_number=3,
        instruction="No further change",
        reviewer_message="Thanks, that's everything",
        creator=REVIEWER,
        now=at(25),
    )

    sent = json.loads(prompts[0])
    items_by_id = {item["item_id"]: item for item in sent["items"]}
    assert "excluded-1" in items_by_id
    assert items_by_id["excluded-1"]["category"] == "team_news"
    assert all(e["item_id"] != "excluded-1" for e in sent["excluded"])


async def test_feedback_counts_as_a_source_for_the_check_step(
    config: Config, store: EditionStore
) -> None:
    edition_id = await make_edition(store)
    # The entry states a fact only found in feedback (the recorded reviewer message), not in
    # any source message, so it must pass rather than fail as an unsupported number or name.
    with_feedback_fact = revision_of(
        ["item-1"],
        draft={
            "intro": "A good week.",
            "sections": [
                {
                    "category": "customer_win",
                    "entries": [
                        {
                            "item_id": "item-1",
                            "text": "Priya Shah signed Northwind Retail for 5 years.",
                            "people": ["Priya Shah"],
                        }
                    ],
                }
            ],
        },
    )
    await store.save_turn(
        edition_id,
        [],
        Feedback(
            reviewer=REVIEWER,
            channel="email",
            text="It's actually a 5 year contract",
            received_at=at(24),
        ),
    )
    agents = runner(config, reviser=scripted([with_feedback_fact]), judge=scripted([JUDGE_PASS]))

    version, failures = await run_revision(
        config,
        store,
        agents,
        edition_id,
        new_version_number=2,
        instruction="Correct the contract length",
        reviewer_message="It's actually a 5 year contract",
        creator=REVIEWER,
        now=at(24),
    )

    assert failures == []
    assert "5 years" in version.draft.sections[0].entries[0].text


async def test_a_failing_revision_is_regenerated_once_with_the_failure_reasons(
    config: Config, store: EditionStore
) -> None:
    edition_id = await make_edition(store)
    bad = revision_of(
        ["item-1"],
        draft={
            "intro": "A good week with 7 wins.",
            "sections": DRAFT_JSON["sections"],
        },
    )
    good = revision_of(["item-1"])
    agents = runner(config, reviser=scripted([bad, good]), judge=scripted([JUDGE_PASS, JUDGE_PASS]))

    version, failures = await run_revision(
        config,
        store,
        agents,
        edition_id,
        new_version_number=2,
        instruction="Tidy the intro",
        reviewer_message="Tidy the intro please",
        creator=REVIEWER,
        now=at(24),
    )

    assert failures == []
    assert version.draft.intro == "A good week."


async def test_second_invalid_reviser_response_raises(config: Config, store: EditionStore) -> None:
    edition_id = await make_edition(store)
    invalid = revision_of(["item-1", "not-a-real-item"])
    agents = runner(config, reviser=scripted([invalid, invalid]), judge=scripted([JUDGE_PASS]))

    with pytest.raises(AgentResponseError) as error:
        await run_revision(
            config,
            store,
            agents,
            edition_id,
            new_version_number=2,
            instruction="x",
            reviewer_message="x",
            creator=REVIEWER,
            now=at(24),
        )
    assert error.value.agent == "reviser"
