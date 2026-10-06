"""Rendered HTML is checked against golden files in golden/. A missing golden file is written
and the test fails, so it is reviewed before it is used; update one by deleting it and
running the test again."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pulse.adapters.render import credit, escape_markdown, labels
from pulse.entities.content import (
    CheckFailure,
    Content,
    Entry,
    EntryNotes,
    NotApplied,
    Section,
    Version,
    entry_credits,
    entry_flags,
)
from pulse.entities.extracts import Sensitivity, make_record
from pulse.entities.review import build_review
from tests.emails import (
    make_consolidation,
    make_email,
    make_item,
    make_output,
    make_verdict,
    screen_email,
)
from tests.operations import make_renderer

GOLDEN = Path(__file__).parent / "golden"

PRIYA = "Priya Shah and Tom Evans signed Northwind Retail on 22 September."
DAN = "Dan Wood thanks Sam Patel for covering the service desk."
CONTENT = Content(
    headline_title="Headline of the week",
    headline="A new retail customer and a thank-you to the service desk",
    intro="A strong week for new customers and some well-earned thanks.",
    # With an empty section, to show it is left out.
    sections=[
        Section(
            category="customer_win",
            title="Customer wins",
            entries=[Entry(item_id="item-1", text=PRIYA, people=[])],
        ),
        Section(
            category="shout_out",
            title="Shout-outs",
            entries=[Entry(item_id="item-2", text=DAN, people=[])],
        ),
        Section(category="team_news", title="Team news", entries=[]),
    ],
    item_ids=["item-1", "item-2"],
)
EMAILS = [
    screen_email(make_email("m01", has_attachments=True)),
    screen_email(
        make_email(
            "m02",
            sender_name="Dan Wood",
            subject="Thank you Sam",
            received=datetime(2026, 9, 23, 23, 30, tzinfo=UTC),
        )
    ),
    screen_email(make_email("m03", sender_name="Alex Morgan", subject="Deal pricing")),
    screen_email(make_email("m04", sender_name="Jo King", subject="Out of office")),
    screen_email(make_email("m05", sender_name="Sam Patel", subject="Next week")),
    screen_email(make_email("m06", subject="Hello", sender_address="someone@example.com")),
]
RECORDS = [
    make_record("m01", make_output(), []),
    make_record(
        "m02",
        make_output(
            category="shout_out",
            sensitivity=[
                Sensitivity(
                    kind="named_person", withheld=False, evidence="thanks Sam Patel by name"
                )
            ],
        ),
        [],
    ),
    make_record(
        "m03",
        make_output(
            sensitivity=[
                Sensitivity(kind="confidential", withheld=True, evidence="an unannounced price")
            ]
        ),
        [],
    ),
    make_record(
        "m04",
        make_output(category=None, exclusion_reason="An out-of-office reply."),
        ["excluded-1"],
    ),
]
CONSOLIDATION = make_consolidation(
    make_item("item-1", source_message_ids=["m01"]),
    make_item("item-2", category="shout_out", source_message_ids=["m02"]),
)
VERSION = Version(
    content=CONTENT,
    changes=["Shortened the intro"],
    not_applied=[NotApplied(feedback="Add the contract value", reason="It is not in the facts")],
    version=2,
    created_at=datetime(2026, 9, 26, 9, 0, tzinfo=UTC),
    check_failures=[CheckFailure(check="digits", target="item-2", detail="3")],
    verdicts=[
        make_verdict("intro"),
        make_verdict(),
        make_verdict("item-2", claim="covering the service desk"),
    ],
    notes=EntryNotes(
        credits=entry_credits(CONTENT, CONSOLIDATION.items, EMAILS),
        flags=entry_flags(CONTENT, CONSOLIDATION.items, RECORDS),
    ),
)
CREDITS = VERSION.notes.credits
# The views reviewers see, and the all-staff send, which has credit lines but no flags.
REVIEWER_VIEWS: list[Callable[[Content], str]] = [
    lambda content: make_renderer().newsletter(content, CREDITS, flags=VERSION.notes.flags),
    lambda content: make_renderer().markdown(content, VERSION.notes),
]
UNFLAGGED_VIEWS: list[Callable[[Content], str]] = [
    lambda content: make_renderer().newsletter(content, CREDITS),
    lambda content: make_renderer().markdown(content, EntryNotes(credits=CREDITS, flags={})),
]


def _assert_golden(name: str, html: str) -> None:
    path = GOLDEN / name
    if not path.exists():
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(html, encoding="utf-8")
        pytest.fail(f"wrote {path.name}; review it, then run the test again")
    assert html == path.read_text(encoding="utf-8")


def test_newsletter_matches_its_golden_file() -> None:
    # The all-staff send: credit lines, and no labels.
    _assert_golden("newsletter.html", make_renderer().newsletter(CONTENT, CREDITS))


def test_flagged_newsletter_matches_its_golden_file() -> None:
    html = make_renderer().newsletter(
        CONTENT, CREDITS, reply="Here is the draft.", flags=VERSION.notes.flags
    )

    _assert_golden("newsletter_flagged.html", html)


def test_reviewer_email_matches_its_golden_file() -> None:
    review = build_review(VERSION, CONSOLIDATION, RECORDS, EMAILS)

    _assert_golden("reviewer_email.html", make_renderer().reviewer_email(VERSION, review))


def test_notice_matches_its_golden_file() -> None:
    _assert_golden(
        "notice.html", make_renderer().notice("Sent", "Version 2 was sent to all staff.")
    )


def test_notice_text_is_escaped() -> None:
    html = make_renderer().notice("<b>Sent</b>", "<script>alert(1)</script>")

    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


REPLY = "I presented **version 2**. Changes:\n\n- Shortened the intro\n- Kept Dan's thank-you"


def test_reply_with_the_reviewer_email_matches_its_golden_file() -> None:
    review = build_review(VERSION, CONSOLIDATION, RECORDS, EMAILS)

    html = make_renderer().reviewer_email(VERSION, review, reply=REPLY)

    _assert_golden("reply_reviewer_email.html", html)


def test_reply_on_its_own_matches_its_golden_file() -> None:
    _assert_golden("reply.html", make_renderer().reply(REPLY))


def test_reply_comes_before_the_newsletter() -> None:
    html = make_renderer().newsletter(CONTENT, CREDITS, reply="Here is the draft.")

    assert html.index("Here is the draft.") < html.index(CONTENT.headline)


@pytest.mark.parametrize(
    ("text", "html"),
    [
        ("**Bold**", "<strong>Bold</strong>"),
        ("- one\n- two", "<li>one</li>"),
        ("See https://example.org/book", '<a href="https://example.org/book">'),
        ("[Booking form](https://example.org/book)", '<a href="https://example.org/book">'),
        ("<b>Bold</b>", "&lt;b&gt;Bold&lt;/b&gt;"),
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
        ("[x](javascript:alert(1))", "[x](javascript:alert(1))"),
    ],
)
def test_reply_markdown_becomes_html_with_raw_html_escaped(text: str, html: str) -> None:
    output = make_renderer().reply(text)

    assert html in output
    assert "<script>" not in output and "<b>" not in output


def test_markdown_newsletter_matches_its_golden_file() -> None:
    markdown = make_renderer().markdown(CONTENT, EntryNotes(credits=CREDITS, flags={}))

    _assert_golden("newsletter.md", markdown)


def test_flagged_markdown_newsletter_matches_its_golden_file() -> None:
    _assert_golden("newsletter_flagged.md", make_renderer().markdown(CONTENT, VERSION.notes))


@pytest.mark.parametrize("render", UNFLAGGED_VIEWS)
def test_unflagged_entries_have_credit_lines_but_no_labels_or_list(
    render: Callable[[Content], str],
) -> None:
    output = render(CONTENT)

    assert "Named person" not in output
    assert "Flagged for review" not in output
    assert "- Priya Shah" in output and "- Dan Wood" in output


@pytest.mark.parametrize(
    ("names", "line"),
    [
        (["Ben Nicholls"], "Ben Nicholls"),
        (["Priya Shah", "Tom Evans"], "Priya Shah and Tom Evans"),
        (["Priya Shah", "Tom Evans", "Aisha Khan"], "Priya Shah, Tom Evans and Aisha Khan"),
    ],
)
def test_credit_names_every_sender(names: list[str], line: str) -> None:
    assert credit(names) == line


def test_labels_name_each_kind_once_and_mark_a_restored_withheld_flag() -> None:
    flags = [
        Sensitivity(kind="financial", withheld=False, evidence="a supplier's prices"),
        Sensitivity(kind="financial", withheld=False, evidence="a deal value"),
        Sensitivity(kind="named_person", withheld=False, evidence="thanks Sam Patel"),
        Sensitivity(kind="personal_information", withheld=True, evidence="a colleague's health"),
    ]

    assert labels(flags) == ["Financial", "Named person", "Restored: personal information"]


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("[Click here](https://example.com)", r"\[Click here\](https://example.com)"),
        ("<img src=x onerror=alert(1)>", r"\<img src=x onerror=alert(1)\>"),
        ("**Bold** and _italic_ and `code`", r"\*\*Bold\*\* and \_italic\_ and \`code\`"),
        ("# Not a heading", r"\# Not a heading"),
        ("- not a list", r"\- not a list"),
        ("1. not a list", r"1\. not a list"),
        ("Priya's thank-you, 22 September.", "Priya's thank-you, 22 September."),
    ],
)
def test_markdown_shows_model_output_as_written(text: str, shown: str) -> None:
    assert escape_markdown(text) == shown


def test_markdown_escapes_every_value() -> None:
    script = "<script>"
    content = CONTENT.model_copy(
        update={
            "headline": script,
            "intro": script,
            "sections": [
                Section(
                    category="customer_win",
                    title="Customer wins",
                    entries=[Entry(item_id="item-1", text=script, people=[])],
                )
            ],
        }
    )

    flags = {"item-1": [Sensitivity(kind="financial", withheld=False, evidence=script)]}

    markdown = make_renderer().markdown(
        content, EntryNotes(credits={"item-1": [script]}, flags=flags)
    )

    assert "<script>" not in markdown
    # The headline, the intro, the entry, its credit line, and the entry in the flagged list
    # with the flag's evidence.
    assert markdown.count(r"\<script\>") == 6


@pytest.mark.parametrize("render", [*UNFLAGGED_VIEWS, *REVIEWER_VIEWS])
def test_sections_render_in_the_order_and_under_the_titles_given(
    render: Callable[[Content], str],
) -> None:
    # A title and section the configuration does not have still render, as the writer gave them.
    content = CONTENT.model_copy(
        update={
            "headline_title": "This week",
            "sections": [
                Section(
                    category="shout_out",
                    title="Thank-yous",
                    entries=[Entry(item_id="item-2", text=DAN, people=[])],
                ),
                Section(
                    category="customer_successes",
                    title="Customer successes",
                    entries=[Entry(item_id="item-1", text=PRIYA, people=[])],
                ),
            ],
        }
    )

    output = render(content)

    assert "This week" in output
    assert output.index("Thank-yous") < output.index("Customer successes") < output.index(PRIYA)
    assert "Headline of the week" not in output


def test_newsletter_has_no_review_section() -> None:
    assert "Review" not in make_renderer().newsletter(CONTENT, CREDITS)


def test_model_output_and_email_text_are_escaped() -> None:
    script = "<script>alert(1)</script>"
    content = CONTENT.model_copy(
        update={
            "headline": script,
            "sections": [
                Section(
                    category="customer_win",
                    title="Customer wins",
                    entries=[Entry(item_id="item-1", text=script, people=[])],
                )
            ],
            "item_ids": ["item-1"],
        }
    )
    emails = [screen_email(make_email("m01", subject=script, has_attachments=True))]
    version = VERSION.model_copy(
        update={
            "content": content,
            "changes": [script],
            "check_failures": [],
            "verdicts": [make_verdict("intro"), make_verdict(claim=script)],
            "notes": EntryNotes(
                credits={"item-1": [script]},
                flags={"item-1": [Sensitivity(kind="financial", withheld=False, evidence=script)]},
            ),
        }
    )
    review = build_review(version, CONSOLIDATION, RECORDS[:1], emails)

    html = make_renderer().reviewer_email(version, review)

    assert "<script>" not in html
    # The headline, the entry and its credit line, the unsupported claim and the entry it names,
    # the flagged entry and its evidence, the change, the attachment, and the source map's text
    # and subject.
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 11
