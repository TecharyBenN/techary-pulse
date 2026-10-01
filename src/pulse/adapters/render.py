"""The newsletter in HTML for email and in Markdown for the chat endpoint, rendered from the
templates with every value escaped."""

import functools
import re
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined

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

    def newsletter(self, content: Content) -> str:
        return self._render(self._html, "newsletter.html.j2", content, None, None)

    def reviewer_email(self, version: Version, review: Review) -> str:
        return self._render(self._html, "newsletter.html.j2", version.content, version, review)

    def notice(self, title: str, text: str) -> str:
        return self._html.get_template("notice.html.j2").render(title=title, text=text)

    def markdown(self, content: Content) -> str:
        return self._render(self._markdown, "newsletter.md.j2", content, None, None)

    def _render(
        self,
        environment: Environment,
        template: str,
        content: Content,
        version: Version | None,
        review: Review | None,
    ) -> str:
        return environment.get_template(template).render(
            content=content, version=version.version if version else None, review=review
        )


def escape_markdown(value: object) -> str:
    """Show the text as written, so model output cannot add formatting, links or HTML."""
    text = _MARKDOWN_INLINE.sub(r"\\\1", str(value))
    return _MARKDOWN_LIST_START.sub(r"\1\\\2", text)
