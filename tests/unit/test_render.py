from datetime import UTC, date, datetime
from pathlib import Path

from pulse.config import load_config
from pulse.models import Draft
from pulse.pipeline.render import Review, Source, render_email, reviewer_subject, subject


def test_subject_uses_run_date(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    assert subject(config, date(2026, 9, 25)) == "Pulse: 25 September 2026"


def test_subject_day_is_not_zero_padded(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    assert subject(config, date(2026, 10, 5)) == "Pulse: 5 October 2026"


def test_reviewer_subject_prefixes_the_version(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    assert reviewer_subject(config, 1, date(2026, 9, 25)) == "Draft v1: Pulse: 25 September 2026"


def test_source_date_in_the_source_map_uses_the_configured_timezone(config_dir: Path) -> None:
    # config.example.yaml sets timezone: Europe/London, which is one hour ahead of UTC in
    # late September, so a message received just before midnight UTC falls on the next day.
    config = load_config(config_dir / "config.example.yaml")
    draft = Draft.model_validate({"intro": "A good week.", "sections": []})
    source = Source(
        sender="Priya Shah", subject="S", received_at=datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    )
    review = Review(source_map=[("headline", [source])])
    html = render_email(config, "headline", draft, review)
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
    review = Review(rejected_subjects=["<img src=x onerror=alert(1)>"])
    html = render_email(config, "<i>headline</i>", draft, review)
    assert "<script>" not in html and "<b>x</b>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html
