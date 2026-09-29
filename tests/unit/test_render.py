from datetime import UTC, date, datetime
from pathlib import Path

from pulse.config import load_config
from pulse.models import Draft, NotApplied, Version
from pulse.pipeline.render import (
    Review,
    render_email,
    render_newsletter,
    render_notice,
    review,
    reviewer_subject,
    subject,
)
from pulse.store import SourceRow

from ..support import DRAFT_V1, ITEM_ROWS, SOURCE_ROWS, at


def source(received_at: datetime) -> SourceRow:
    return SourceRow(
        "m1", "included", "Signed Northwind Retail", "Priya Shah", received_at=received_at
    )


GOLDEN = Path(__file__).parent.parent / "golden"

TWO_SECTION_DRAFT = Draft.model_validate(
    {
        "intro": "A strong week for new customers and some well-earned thanks.",
        "sections": [
            {
                "category": "customer_win",
                "entries": [
                    {
                        "item_id": "item-1",
                        "text": (
                            "Priya Shah and Tom Evans signed Northwind Retail, with onboarding "
                            "starting in October."
                        ),
                        "people": ["Priya Shah", "Tom Evans"],
                    }
                ],
            },
            {
                "category": "shout_out",
                "entries": [
                    {
                        "item_id": "item-2",
                        "text": "Thanks to Sam Patel for covering the service desk all weekend.",
                        "people": ["Sam Patel"],
                    }
                ],
            },
        ],
    }
)
TWO_SECTION_HEADLINE = "A new retail customer and a thank-you to the service desk"


def test_subject_uses_run_date(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    assert subject(config, date(2026, 9, 25)) == "Pulse: 25 September 2026"


def test_subject_day_is_not_zero_padded(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    assert subject(config, date(2026, 10, 5)) == "Pulse: 5 October 2026"


def test_reviewer_subject_prefixes_the_version_and_uses_the_local_creation_date(
    config_dir: Path,
) -> None:
    # 23:30 UTC on 24 September is 00:30 on the 25th in Europe/London.
    config = load_config(config_dir / "config.example.yaml")
    created_at = datetime(2026, 9, 24, 23, 30, tzinfo=UTC)
    assert reviewer_subject(config, 2, created_at) == "Draft v2: Pulse: 25 September 2026"


def test_review_is_built_from_the_editions_items_and_sources() -> None:
    version = Version(
        number=1,
        draft=DRAFT_V1,
        headline="h",
        item_ids=["item-1"],
        check_results=[],
        creator="pulse",
        created_at=at(22),
    )
    sources = {row.message_id: row for row in SOURCE_ROWS}
    result = review(version, ITEM_ROWS, sources)
    assert result.sensitivity_exclusions == [(sources["m02"], "personal", "mentions a promotion")]
    assert result.other_exclusions == []
    assert result.source_map == [("Priya Shah signed Northwind Retail.", [sources["m01"]])]


def test_source_date_in_the_source_map_uses_the_configured_timezone(config_dir: Path) -> None:
    # config.example.yaml sets timezone: Europe/London, which is one hour ahead of UTC in
    # late September, so a message received just before midnight UTC falls on the next day.
    config = load_config(config_dir / "config.example.yaml")
    draft = Draft.model_validate({"intro": "A good week.", "sections": []})
    late = source(datetime(2026, 9, 30, 23, 30, tzinfo=UTC))
    html = render_email(config, "headline", draft, Review(source_map=[("headline", [late])]))
    assert "1 October 2026" in html


def test_model_and_email_text_is_escaped(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    draft = Draft.model_validate(
        {
            "intro": "<script>alert(1)</script>",
            "sections": [
                {
                    "category": "shout_out",
                    "entries": [{"item_id": "i", "text": "<b>x</b>", "people": []}],
                }
            ],
        }
    )
    rejected = Review(rejected_subjects=["<img src=x onerror=alert(1)>"])
    html = render_email(config, "<i>headline</i>", draft, rejected)
    assert "<script>" not in html and "<b>x</b>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html


def test_newsletter_matches_golden_file_and_has_no_review_section(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    html = render_newsletter(config, TWO_SECTION_HEADLINE, TWO_SECTION_DRAFT)
    assert html == (GOLDEN / "newsletter.html").read_text(encoding="utf-8")
    assert "Review" not in html


def test_notice_matches_golden_file() -> None:
    html = render_notice("Sent", "Version 2 was sent to all staff.")
    assert html == (GOLDEN / "notice.html").read_text(encoding="utf-8")


def test_revision_reviewer_email_matches_golden_file(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    revision_review = Review(
        changes=["Shortened the headline", "Moved the service desk thanks to shout-outs"],
        not_applied=[
            NotApplied(
                feedback="Add the contract value",
                reason="The item is excluded for commercial sensitivity",
            )
        ],
        source_map=[("item-1", [source(datetime(2026, 9, 22, 9, tzinfo=UTC))])],
    )
    html = render_email(config, TWO_SECTION_HEADLINE, TWO_SECTION_DRAFT, revision_review)
    assert html == (GOLDEN / "revision_reviewer_email.html").read_text(encoding="utf-8")
    assert "Changes" in html and "Feedback not applied" in html
