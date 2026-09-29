"""Subjects, the newsletter, its review section, and the emails built from them."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader

from pulse.config import Config
from pulse.models import Draft, NotApplied, OutgoingEmail, Version
from pulse.pipeline.rules import sections_in_order
from pulse.store import ItemRow, SourceRow


def _format_date(day: date) -> str:
    """Render a date like "25 September 2026", the design's one date format."""
    return f"{day.day} {day:%B %Y}"


def _local_date(received_at: datetime, timezone: str) -> str:
    """Render a UTC timestamp as a date in the given IANA time zone."""
    return _format_date(received_at.astimezone(ZoneInfo(timezone)).date())


_env = Environment(loader=PackageLoader("pulse", "templates"), autoescape=True)
_env.filters["local_date"] = _local_date


@dataclass(frozen=True)
class Review:
    """The review section's parts, in the order the design lists them."""

    check_failures: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    not_applied: list[NotApplied] = field(default_factory=list)
    sensitivity_exclusions: list[tuple[SourceRow, str, str]] = field(default_factory=list)
    other_exclusions: list[tuple[SourceRow, str]] = field(default_factory=list)
    rejected_subjects: list[str] = field(default_factory=list)
    with_attachments: list[SourceRow] = field(default_factory=list)
    source_map: list[tuple[str, list[SourceRow]]] = field(default_factory=list)


def review(
    version: Version, item_rows: Sequence[ItemRow], sources: Mapping[str, SourceRow]
) -> Review:
    """Build a version's review section from its edition's items and sources.

    ``sources`` is in received order, as the build saved it, and orders the rejected and
    attachment lists.
    """
    rows = {row.item_id: row for row in item_rows}
    entry_text = {e.item_id: e.text for s in version.draft.sections for e in s.entries}
    excluded = [row.excluded() for row in item_rows if row.kind == "excluded"]
    return Review(
        check_failures=version.check_results,
        changes=version.changes,
        not_applied=version.not_applied,
        sensitivity_exclusions=[
            (sources[record.message_id], s.type, s.evidence)
            for record, _ in excluded
            for s in record.sensitivity
        ],
        other_exclusions=[
            (sources[record.message_id], reason)
            for record, reason in excluded
            if reason != "sensitivity"
        ],
        rejected_subjects=[s.subject for s in sources.values() if s.outcome == "rejected"],
        with_attachments=[
            s for s in sources.values() if s.has_attachments and s.outcome != "rejected"
        ],
        source_map=[
            (
                entry_text.get(item_id, item_id),
                [sources[i] for i in rows[item_id].source_message_ids],
            )
            for item_id in version.item_ids
        ],
    )


def subject(config: Config, run_date: date) -> str:
    return config.subject_template.format(date=_format_date(run_date))


def edition_subject(config: Config, created_at: datetime) -> str:
    """An edition's own subject, from its creation date in the configured time zone."""
    return subject(config, created_at.astimezone(ZoneInfo(config.timezone)).date())


def reviewer_subject(config: Config, version: int, created_at: datetime) -> str:
    return f"Draft v{version}: {edition_subject(config, created_at)}"


def visible_text(config: Config, headline: str, draft: Draft) -> list[str]:
    """The newsletter text a reader sees, excluding the review section."""
    text = [config.headline_title, headline, draft.intro]
    for section, entries in sections_in_order(draft, config):
        text.append(section.title)
        text.extend(entry.text for entry in entries)
    return text


def _render_newsletter(config: Config, headline: str, draft: Draft, review: Review | None) -> str:
    return _env.get_template("newsletter.html.j2").render(
        headline_title=config.headline_title,
        headline=headline,
        intro=draft.intro,
        sections=sections_in_order(draft, config),
        review=review,
        timezone=config.timezone,
    )


def render_email(config: Config, headline: str, draft: Draft, review: Review) -> str:
    """Render the reviewer email: the newsletter followed by its review section."""
    return _render_newsletter(config, headline, draft, review)


def render_newsletter(config: Config, headline: str, draft: Draft) -> str:
    """Render the newsletter for all staff, without the review section."""
    return _render_newsletter(config, headline, draft, None)


def render_notice(title: str, text: str) -> str:
    """Render a short, escaped notice or reply."""
    return _env.get_template("notice.html.j2").render(title=title, text=text)


def version_email(
    config: Config,
    created_at: datetime,
    version: Version,
    item_rows: Sequence[ItemRow],
    sources: Mapping[str, SourceRow],
) -> OutgoingEmail:
    """The reviewer email for a version of the edition created at ``created_at``."""
    html = render_email(
        config, version.headline, version.draft, review(version, item_rows, sources)
    )
    return OutgoingEmail(
        to=config.reviewers,
        subject=reviewer_subject(config, version.number, created_at),
        html=html,
    )


def notice_email(config: Config, created_at: datetime, title: str, text: str) -> OutgoingEmail:
    """A notice to reviewers about the edition created at ``created_at``, such as "Sent"."""
    return OutgoingEmail(
        to=config.reviewers,
        subject=f"{title}: {edition_subject(config, created_at)}",
        html=render_notice(title, text),
    )
