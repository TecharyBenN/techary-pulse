"""Rendering: Renderer turns content into the newsletter's HTML and Markdown, and the orchestrator's
replies into HTML."""

import functools
import re
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined
from markdown_it import MarkdownIt
from markupsafe import Markup

from pulse.entities.content import (
    Content,
    EntryCredits,
    EntryFlags,
    EntryNotes,
    Version,
    flagged_entries,
)
from pulse.entities.extracts import Sensitivity, SensitivityKind
from pulse.entities.mail import display_date
from pulse.entities.review import Review

# Characters that start Markdown formatting, links or HTML anywhere in a line.
_MARKDOWN_INLINE = re.compile(r"([\\`*_\[\]<>#|~])")
# A list marker at the start of a line; Markdown escapes only punctuation, so the
# punctuation after a number is escaped, not the number.
_MARKDOWN_LIST_START = re.compile(r"^(\s*(?:[0-9]+)?)([-+.)])", re.MULTILINE)
# How reviewers see each kind of sensitivity flag.
_KIND_LABELS: dict[SensitivityKind, str] = {
    "named_person": "Named person",
    "personal_information": "Personal information",
    "financial": "Financial",
    "confidential": "Confidential",
    "inappropriate": "Inappropriate",
}


class Renderer:
    """Renders the newsletter, reviewer emails, notices and replies from the templates."""

    def __init__(self, timezone: ZoneInfo) -> None:
        self._html = Environment(
            loader=PackageLoader("pulse.adapters"), autoescape=True, undefined=StrictUndefined
        )
        self._html.filters["display_date"] = functools.partial(display_date, timezone=timezone)
        # Markdown has no autoescaping, so every value is escaped as it is output.
        self._markdown = Environment(
            loader=PackageLoader("pulse.adapters"),
            undefined=StrictUndefined,
            finalize=escape_markdown,
            keep_trailing_newline=True,
        )
        for environment in (self._html, self._markdown):
            environment.filters |= {
                "credit": credit,
                "kind": _KIND_LABELS.__getitem__,
                "label": label,
                "labels": labels,
            }
        # Raw HTML in a reply is escaped by the converter, so only its output is marked safe.
        self._reply_markdown = MarkdownIt("commonmark", {"html": False, "linkify": True})
        self._reply_markdown.enable("linkify")

    def newsletter(
        self,
        content: Content,
        credits: EntryCredits,
        reply: str | None = None,
        flags: EntryFlags | None = None,
    ) -> str:
        """`reply` is the orchestrator's reply in Markdown, shown before the newsletter. `flags`
        labels the entries flagged for the reviewers; the all-staff send passes none."""
        return self._render(
            self._html, "newsletter.html.j2", content, credits, flags or {}, None, None, reply
        )

    def reviewer_email(self, version: Version, review: Review, reply: str | None = None) -> str:
        return self._render(
            self._html,
            "newsletter.html.j2",
            version.content,
            version.notes.credits,
            version.notes.flags,
            version,
            review,
            reply,
        )

    def notice(self, title: str, text: str) -> str:
        return self._notice(title, text, None)

    def reply(self, text: str) -> str:
        """The orchestrator's reply in Markdown, on its own."""
        return self._notice(None, None, text)

    def markdown(self, content: Content, notes: EntryNotes) -> str:
        """The newsletter a reviewer sees in chat, so it always shows its flags."""
        return self._render(
            self._markdown, "newsletter.md.j2", content, notes.credits, notes.flags, None, None
        )

    def _notice(self, title: str | None, text: str | None, reply: str | None) -> str:
        return self._html.get_template("notice.html.j2").render(
            title=title, text=text, reply=self._reply_html(reply)
        )

    def _render(
        self,
        environment: Environment,
        template: str,
        content: Content,
        credits: EntryCredits,
        flags: EntryFlags,
        version: Version | None,
        review: Review | None,
        reply: str | None = None,
    ) -> str:
        return environment.get_template(template).render(
            content=content,
            credits=credits,
            flags=flags,
            flagged=flagged_entries(content, flags),
            version=version.version if version else None,
            review=review,
            reply=self._reply_html(reply),
        )

    def _reply_html(self, reply: str | None) -> Markup | None:
        return Markup(self._reply_markdown.render(reply)) if reply else None


def escape_markdown(value: object) -> str:
    """Show the text as written, so model output cannot add formatting, links or HTML."""
    text = _MARKDOWN_INLINE.sub(r"\\\1", str(value))
    return _MARKDOWN_LIST_START.sub(r"\1\\\2", text)


def credit(names: Sequence[str]) -> str:
    """Names as a credit line gives them: A, A and B, or A, B and C."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def label(flag: Sensitivity) -> str:
    """A flag as reviewers see it. Only a restored record carries a withheld flag into a
    newsletter, so its label says so."""
    kind = _KIND_LABELS[flag.kind]
    return f"Restored: {kind.lower()}" if flag.withheld else kind


def labels(flags: Sequence[Sensitivity]) -> list[str]:
    """An entry's labels, each once."""
    return list(dict.fromkeys(label(flag) for flag in flags))
