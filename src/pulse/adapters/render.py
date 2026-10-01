"""The newsletter in HTML for email and in Markdown for the chat endpoint, rendered from the
templates with every value escaped, and the orchestrator's replies in HTML for email."""

import functools
import re
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined
from markdown_it import MarkdownIt
from markupsafe import Markup

from pulse.entities.content import Content, Version
from pulse.entities.mail import display_date
from pulse.entities.review import Review

# Characters that start Markdown formatting, links or HTML anywhere in a line.
_MARKDOWN_INLINE = re.compile(r"([\\`*_\[\]<>#|~])")
# A list marker at the start of a line; Markdown escapes only punctuation, so the
# punctuation after a number is escaped, not the number.
_MARKDOWN_LIST_START = re.compile(r"^(\s*(?:[0-9]+)?)([-+.)])", re.MULTILINE)


class Renderer:
    """Renders content as the writer structured it: its titles, in its order."""

    def __init__(self, timezone: ZoneInfo) -> None:
        # Model output and email-derived text are escaped; nothing is ever marked safe.
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
        # Raw HTML in a reply is escaped by the converter, so only its output is marked safe.
        self._reply_markdown = MarkdownIt("commonmark", {"html": False, "linkify": True})
        self._reply_markdown.enable("linkify")

    def newsletter(self, content: Content, reply: str | None = None) -> str:
        """`reply` is the orchestrator's reply in Markdown, shown before the newsletter."""
        return self._render(self._html, "newsletter.html.j2", content, None, None, reply)

    def reviewer_email(self, version: Version, review: Review, reply: str | None = None) -> str:
        return self._render(
            self._html, "newsletter.html.j2", version.content, version, review, reply
        )

    def notice(self, title: str, text: str) -> str:
        return self._notice(title, text, None)

    def reply(self, text: str) -> str:
        """The orchestrator's reply in Markdown, on its own."""
        return self._notice(None, None, text)

    def markdown(self, content: Content) -> str:
        return self._render(self._markdown, "newsletter.md.j2", content, None, None)

    def _notice(self, title: str | None, text: str | None, reply: str | None) -> str:
        return self._html.get_template("notice.html.j2").render(
            title=title, text=text, reply=self._reply_html(reply)
        )

    def _render(
        self,
        environment: Environment,
        template: str,
        content: Content,
        version: Version | None,
        review: Review | None,
        reply: str | None = None,
    ) -> str:
        return environment.get_template(template).render(
            content=content,
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
