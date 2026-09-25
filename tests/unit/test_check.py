from pathlib import Path

import pytest

from pulse.config import Config, load_config
from pulse.models import Draft, ItemWithSenders, JudgeResult
from pulse.pipeline.check import EM_DASH, EN_DASH, check_draft, judge_failures

from ..support import message

MESSAGES = {
    "m1": message(
        "m1", body="Tom Evans and I signed Northwind Retail on 22 September for 3 years."
    ),
    "m2": message("m2", sender_name="Sam Patel", body="Covered the service desk all weekend."),
}
ITEMS = [
    ItemWithSenders(
        item_id="i1",
        category="customer_win",
        facts=["Signed Northwind Retail"],
        people=["Priya Shah", "Tom Evans"],
        source_message_ids=["m1"],
        sender_names=["Priya Shah"],
    ),
    ItemWithSenders(
        item_id="i2",
        category="shout_out",
        facts=["Covered the service desk"],
        people=["Sam Patel"],
        source_message_ids=["m2"],
        sender_names=["Sam Patel"],
    ),
]
GOOD_I1 = "Priya Shah and Tom Evans signed Northwind Retail on 22 September for 3 years."
GOOD_I2 = "Thanks to Sam Patel for covering the service desk."


@pytest.fixture
def config(config_dir: Path) -> Config:
    return load_config(config_dir / "config.example.yaml")


def draft(
    i1: str = GOOD_I1, i1_people: list[str] | None = None, intro: str = "A good week."
) -> Draft:
    return Draft.model_validate(
        {
            "intro": intro,
            "sections": [
                {
                    "category": "customer_win",
                    "entries": [
                        {
                            "item_id": "i1",
                            "text": i1,
                            "people": i1_people or ["Priya Shah", "Tom Evans"],
                        }
                    ],
                },
                {
                    "category": "shout_out",
                    "entries": [{"item_id": "i2", "text": GOOD_I2, "people": ["Sam Patel"]}],
                },
            ],
        }
    )


def check(config: Config, d: Draft) -> list[str]:
    return check_draft(d, "A new customer", ITEMS, MESSAGES, config)


def test_good_draft_passes(config: Config) -> None:
    assert check(config, draft()) == []


def test_word_limit_counts_visible_text(config: Config) -> None:
    short = config.model_copy(update={"limits": config.limits.model_copy(update={"max_words": 10})})
    assert any("more than 10" in f for f in check(short, draft()))


def test_dash_fails_the_check(config: Config) -> None:
    for dash in (EM_DASH, EN_DASH):
        assert "draft uses an em dash or en dash" in check(
            config, draft(intro=f"A week {dash} busy.")
        )


@pytest.mark.parametrize(
    ("kwargs", "failure"),
    [
        ({"i1": GOOD_I1.replace("3 years", "5 years")}, "number 5 is not in its sources"),
        ({"intro": "Four wins in 7 days."}, "intro number 7"),
        ({"i1_people": ["Priya Shah", "Tom Evans", "Ann Lee"]}, "Ann Lee is listed but not named"),
        (
            {
                "i1": GOOD_I1 + " Ann Lee helped.",
                "i1_people": ["Priya Shah", "Tom Evans", "Ann Lee"],
            },
            "Ann Lee is not in its sources or senders",
        ),
        (
            {
                "i1": "Tom Evans signed Northwind Retail on 22 September for 3 years.",
                "i1_people": ["Tom Evans"],
            },
            "sender Priya Shah is not named",
        ),
        ({"i1": GOOD_I1 + " Great work. Well done!"}, "more than two sentences"),
    ],
)
def test_check_failures(config: Config, kwargs: dict[str, object], failure: str) -> None:
    assert any(failure in f for f in check(config, draft(**kwargs))), check(config, draft(**kwargs))  # type: ignore[arg-type]


def test_names_match_ignoring_case(config: Config) -> None:
    assert check(config, draft(i1=GOOD_I1.replace("Tom Evans", "tom evans"))) == []


def test_items_must_appear_exactly_once_in_configured_sections(config: Config) -> None:
    d = draft()
    d.sections[1].entries.clear()
    d.sections.append(d.sections[0].model_copy(update={"category": "birthdays"}))
    failures = check(config, d)
    assert "item i2 appears 0 times, not once" in failures
    assert "item i1 appears 2 times, not once" in failures
    assert "category birthdays is not configured" in failures


def test_judge_failures() -> None:
    result = JudgeResult.model_validate(
        {
            "intro": {"supported": False, "reason": "claims four wins"},
            "entries": [
                {"item_id": "i1", "supported": True, "reason": ""},
                {"item_id": "i2", "supported": False, "reason": "no date in facts"},
            ],
        }
    )
    assert judge_failures(result) == ["intro: claims four wins", "i2: no date in facts"]
