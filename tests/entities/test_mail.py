from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from pulse.adapters.graph import GraphMailbox
from pulse.entities.mail import (
    Body,
    Email,
    InboundEmail,
    Mailbox,
    OutboundEmail,
    address_in,
    display_date,
    domain_in,
    header_value,
    is_automatic_reply,
    screen,
    sender_names,
    source_text,
    subject,
)
from tests.emails import make_email, make_screened_email
from tests.fakes.graph import MAILBOX, FakeGraph
from tests.fakes.mailbox import FakeMailbox

LABEL = "3a1b7f0e-5c2d-4e8a-9b6f-1d2c3e4f5a6b"
OTHER_LABEL = "9c8d7e6f-0000-4e8a-9b6f-1d2c3e4f5a6b"


def test_address_in_ignores_case() -> None:
    assert address_in("Priya.Shah@Example.ORG", ["priya.shah@example.org"])


def test_address_in_is_exact() -> None:
    assert not address_in("priya.shah@example.org.example.com", ["priya.shah@example.org"])


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("priya.shah@example.org", True),
        ("priya.shah@EXAMPLE.ORG", True),
        ("priya.shah@mail.example.org", False),
        ("priya.shah@noexample.org", False),
        ("example.org", False),
    ],
)
def test_domain_in(address: str, expected: bool) -> None:
    assert domain_in(address, ["Example.org"]) is expected


def test_header_value_ignores_name_case() -> None:
    assert header_value({"Auto-Submitted": "auto-replied"}, "auto-submitted") == "auto-replied"


def test_header_value_absent() -> None:
    assert header_value({}, "auto-submitted") is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, False),
        ({"Auto-Submitted": "no"}, False),
        ({"auto-submitted": " No "}, False),
        ({"Auto-Submitted": "auto-replied"}, True),
        ({"Auto-Submitted": "auto-generated"}, True),
        ({"X-Auto-Response-Suppress": "All"}, True),
        ({"x-auto-response-suppress": ""}, True),
    ],
)
def test_is_automatic_reply(headers: dict[str, str], expected: bool) -> None:
    assert is_automatic_reply(headers) is expected


# Interface tests every mailbox implementation must pass.

MailboxFactory = Callable[[list[InboundEmail]], Mailbox]


def _email(message_id: str, day: int, **changes: object) -> InboundEmail:
    return make_email(message_id, received=datetime(2026, 9, day, 15, 30, tzinfo=UTC), **changes)


async def _token() -> str:
    return "token-1"


def _graph_mailbox(inbox: list[InboundEmail]) -> Mailbox:
    return GraphMailbox(FakeGraph(inbox).client(), _token, MAILBOX, max_retries=2)


@pytest.fixture(params=["fake", "graph"])
def make_mailbox(request: pytest.FixtureRequest) -> MailboxFactory:
    factories: dict[str, MailboxFactory] = {"fake": FakeMailbox, "graph": _graph_mailbox}
    return factories[request.param]


@pytest.mark.anyio
async def test_empty_inbox(make_mailbox: MailboxFactory) -> None:
    assert await make_mailbox([]).list_inbox() == []


@pytest.mark.anyio
async def test_inbox_returns_every_field(make_mailbox: MailboxFactory) -> None:
    email = _email("m01", 22, has_attachments=True, headers={"X-Auto-Response-Suppress": "All"})

    assert await make_mailbox([email]).list_inbox() == [email]


@pytest.mark.anyio
async def test_inbox_is_sorted_by_received_time(make_mailbox: MailboxFactory) -> None:
    emails = [_email("m03", 24), _email("m01", 22), _email("m02", 23)]

    listed = await make_mailbox(emails).list_inbox()

    assert [email.message_id for email in listed] == ["m01", "m02", "m03"]


@pytest.mark.anyio
async def test_move_removes_only_that_message(make_mailbox: MailboxFactory) -> None:
    mailbox = make_mailbox([_email("m01", 22), _email("m02", 23)])

    await mailbox.move("m01", "Processed")

    assert [email.message_id for email in await mailbox.list_inbox()] == ["m02"]


_HTML = Body(content="<p>Hello</p>", content_type="html")


@pytest.mark.anyio
async def test_send_returns_the_message_id(make_mailbox: MailboxFactory) -> None:
    email = OutboundEmail(
        to=["all-staff@example.org"],
        subject="Pulse: 25 September 2026",
        body=_HTML,
        reply_to="pulse@example.org",
    )

    assert await make_mailbox([]).send(email)


@pytest.mark.anyio
async def test_reply_returns_its_own_id(make_mailbox: MailboxFactory) -> None:
    mailbox = make_mailbox([_email("m01", 22)])
    text = Body(content="Version 2 is on its way.", content_type="text")

    reply_id = await mailbox.reply("m01", ["reviewer@example.org"], text)

    assert reply_id and reply_id != "m01"


@pytest.mark.anyio
async def test_a_sent_message_can_be_replied_to(make_mailbox: MailboxFactory) -> None:
    mailbox = make_mailbox([])
    email = OutboundEmail(to=["reviewer@example.org"], subject="Draft", body=_HTML, reply_to=None)
    sent_id = await mailbox.send(email)

    reply_id = await mailbox.reply(sent_id, ["reviewer@example.org"], _HTML)

    assert reply_id not in (sent_id, "")


def _sender_email(
    sender_address: str = "priya.shah@example.org", headers: dict[str, str] | None = None
) -> InboundEmail:
    return make_email(sender_address=sender_address, headers=headers or {}, has_attachments=True)


def _screen(
    email: InboundEmail, senders: list[str] | None = None, labels: list[str] | None = None
) -> str | None:
    return screen(email, ["example.org"], senders or [], labels or []).rejection


def test_screened_email_is_the_email_with_its_outcome() -> None:
    email = _sender_email()

    screened = screen(email, ["example.org"], [], [])

    assert isinstance(screened, Email)
    assert screened.model_dump(include=set(Email.model_fields)) == email.model_dump(
        include=set(Email.model_fields)
    )


def test_passing_screened_email_keeps_its_fields_and_body() -> None:
    screened = screen(_sender_email(), ["example.org"], [], [])

    assert screened.rejection is None
    assert screened.message_id == "m01"
    assert screened.sender_name == "Priya Shah"
    assert screened.sender_address == "priya.shah@example.org"
    assert screened.subject == "Signed Northwind Retail today"
    assert screened.received == datetime(2026, 9, 22, 15, 30, tzinfo=UTC)
    assert screened.has_attachments
    assert screened.body == "Tom Evans and I signed Northwind Retail on 22 September."


def test_rejected_screened_email_drops_its_body() -> None:
    screened = screen(_sender_email("alex.morgan@example.com"), ["example.org"], [], [])

    assert screened.rejection == "sender_domain"
    assert screened.body is None
    assert screened.subject == "Signed Northwind Retail today"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("priya.shah@example.org", None),
        ("Priya.Shah@EXAMPLE.org", None),
        ("alex.morgan@example.com", "sender_domain"),
        ("alex.morgan@mail.example.org", "sender_domain"),
        ("alex.morgan@example.org.example.com", "sender_domain"),
        ("no-domain", "sender_domain"),
    ],
)
def test_sender_domain(address: str, expected: str | None) -> None:
    assert _screen(_sender_email(address)) == expected


def test_empty_allowed_senders_allows_any_sender_in_domain() -> None:
    assert _screen(_sender_email("anyone@example.org"), senders=[]) is None


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("priya.shah@example.org", None),
        ("PRIYA.SHAH@example.org", None),
        ("tom.evans@example.org", "sender_not_allowed"),
    ],
)
def test_allowed_senders(address: str, expected: str | None) -> None:
    assert _screen(_sender_email(address), senders=["priya.shah@example.org"]) == expected


def test_domain_is_checked_before_allowed_senders() -> None:
    email = _sender_email("alex.morgan@example.com")

    assert _screen(email, senders=["alex.morgan@example.com"]) == "sender_domain"


def _labels(value: str) -> InboundEmail:
    return _sender_email(headers={"msip_labels": value})


@pytest.mark.parametrize(
    ("header", "allowed", "expected"),
    [
        (f"MSIP_Label_{LABEL}_Enabled=true", [], "sensitivity_label"),
        (f"MSIP_Label_{LABEL}_Enabled=TRUE", [], "sensitivity_label"),
        (f"MSIP_Label_{LABEL}_Enabled=true", [LABEL], None),
        (f"MSIP_Label_{LABEL}_Enabled=true", [LABEL.upper()], None),
        (f"MSIP_Label_{LABEL}_Enabled=false", [], None),
        (f"MSIP_Label_{LABEL}_Name=Confidential; MSIP_Label_{LABEL}_SiteId=x", [], None),
        (
            f"MSIP_Label_{LABEL}_Enabled=true; MSIP_Label_{LABEL}_Name=General;"
            f" MSIP_Label_{OTHER_LABEL}_Enabled=true;",
            [LABEL],
            "sensitivity_label",
        ),
        (
            f"MSIP_Label_{LABEL}_Enabled=true; MSIP_Label_{OTHER_LABEL}_Enabled=true",
            [LABEL, OTHER_LABEL],
            None,
        ),
        ("", [], None),
        ("not a label entry", [LABEL], "sensitivity_label"),
        (f"MSIP_Label_{LABEL}_Enabled=true; garbage", [LABEL], "sensitivity_label"),
    ],
)
def test_sensitivity_labels(header: str, allowed: list[str], expected: str | None) -> None:
    assert _screen(_labels(header), labels=allowed) == expected


def test_label_header_name_ignores_case() -> None:
    email = _sender_email(headers={"MSIP_Labels": f"MSIP_Label_{LABEL}_Enabled=true"})

    assert _screen(email) == "sensitivity_label"


def test_unlabelled_message_is_allowed() -> None:
    assert _screen(_sender_email(), labels=[LABEL]) is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Auto-Submitted": "no"}, None),
        ({"Auto-Submitted": "auto-replied"}, "automatic_reply"),
        ({"X-Auto-Response-Suppress": "All"}, "automatic_reply"),
    ],
)
def test_automatic_replies(headers: dict[str, str], expected: str | None) -> None:
    assert _screen(_sender_email(headers=headers)) == expected


def test_source_text_is_subject_and_body() -> None:
    text = source_text(screen(_sender_email(), ["example.org"], [], []))

    assert "Signed Northwind Retail today" in text
    assert "22 September" in text


def test_source_text_of_rejected_screened_email_is_subject_only() -> None:
    text = source_text(screen(_sender_email("alex.morgan@example.com"), ["example.org"], [], []))

    assert text == "Signed Northwind Retail today"


def test_sender_names_are_each_sender_once_in_order() -> None:
    emails = [
        make_screened_email("m01", sender_name="Tom Evans"),
        make_screened_email("m02"),
        make_screened_email("m03", sender_name="Tom Evans"),
    ]

    assert sender_names(emails) == ["Tom Evans", "Priya Shah"]


LONDON = ZoneInfo("Europe/London")


def test_display_date_is_the_date_in_the_time_zone() -> None:
    # 23:30 UTC is already the next day in London during British Summer Time.
    assert display_date(datetime(2026, 9, 25, 23, 30, tzinfo=UTC), LONDON) == "26 September 2026"


def test_subject_fills_in_the_opening_date_after_the_prefix() -> None:
    opened = datetime(2026, 9, 5, 16, 30, tzinfo=UTC)

    assert subject("Pulse: {date}", opened, LONDON) == "Pulse: 5 September 2026"
    assert subject("Pulse: {date}", opened, LONDON, "Draft v2:") == (
        "Draft v2: Pulse: 5 September 2026"
    )
