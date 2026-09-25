from datetime import date
from pathlib import Path

from pulse.config import load_config
from pulse.models import Draft
from pulse.pipeline.render import Review, render_email, subject


def test_subject_uses_run_date(config_dir: Path) -> None:
    config = load_config(config_dir / "config.example.yaml")
    assert subject(config, date(2026, 9, 25)) == "Pulse: week ending 25 September 2026"


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
