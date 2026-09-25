"""Step 8: the subject, the newsletter and the review section."""

from dataclasses import dataclass, field
from datetime import date

from jinja2 import Environment, PackageLoader

from pulse.config import Config
from pulse.models import Draft
from pulse.pipeline.rules import sections_in_order

_env = Environment(loader=PackageLoader("pulse", "templates"), autoescape=True)


@dataclass(frozen=True)
class Source:
    sender: str
    subject: str


@dataclass(frozen=True)
class Review:
    """The review section's six parts, in the order the design lists them."""

    check_failures: list[str] = field(default_factory=list)
    sensitivity_exclusions: list[tuple[Source, str, str]] = field(default_factory=list)
    other_exclusions: list[tuple[Source, str]] = field(default_factory=list)
    rejected_subjects: list[str] = field(default_factory=list)
    with_attachments: list[Source] = field(default_factory=list)
    source_map: list[tuple[str, list[Source]]] = field(default_factory=list)


def subject(config: Config, run_date: date) -> str:
    week_ending = f"{run_date.day} {run_date:%B %Y}"
    return config.subject_template.format(week_ending=week_ending)


def visible_text(config: Config, headline: str, draft: Draft) -> list[str]:
    """The newsletter text a reader sees, excluding the review section."""
    text = [config.headline_title, headline, draft.intro]
    for section, entries in sections_in_order(draft, config):
        text.append(section.title)
        text.extend(entry.text for entry in entries)
    return text


def render_email(config: Config, headline: str, draft: Draft, review: Review) -> str:
    return _env.get_template("newsletter.html.j2").render(
        headline_title=config.headline_title,
        headline=headline,
        intro=draft.intro,
        sections=sections_in_order(draft, config),
        review=review,
    )
