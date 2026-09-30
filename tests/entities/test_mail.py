from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from pulse.adapters.graph import GraphMailbox
from pulse.entities.mail import (
    InboundEmail,
    Mailbox,
    OutboundEmail,
    address_in,
    domain_in,
    header_value,
    is_automatic_reply,
)
from tests.emails import make_email
from tests.fakes.graph import MAILBOX, FakeGraph
from tests.fakes.mailbox import FakeMailbox


def test_address_in_ignores_case() -> None:
    assert address_in("Priya.Shah@Techary.AI", ["priya.shah@techary.ai"])


def test_address_in_is_exact() -> None:
    assert not address_in("priya.shah@techary.ai.example.com", ["priya.shah@techary.ai"])


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("priya.shah@techary.ai", True),
        ("priya.shah@TECHARY.AI", True),
        ("priya.shah@mail.techary.ai", False),
        ("priya.shah@notechary.ai", False),
        ("techary.ai", False),
    ],
)
def test_domain_in(address: str, expected: bool) -> None:
    assert domain_in(address, ["Techary.ai"]) is expected


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


@pytest.mark.anyio
async def test_send_is_accepted(make_mailbox: MailboxFactory) -> None:
    email = OutboundEmail(
        to=["all-staff@techary.ai"],
        subject="Pulse: 25 September 2026",
        html="<p>Hello</p>",
        reply_to="pulse@techary.ai",
    )

    await make_mailbox([]).send(email)


@pytest.mark.anyio
async def test_reply_is_accepted(make_mailbox: MailboxFactory) -> None:
    mailbox = make_mailbox([_email("m01", 22)])

    await mailbox.reply("m01", ["reviewer@techary.ai"], "Version 2 is on its way.")
