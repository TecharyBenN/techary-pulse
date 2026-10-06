import datetime as dt
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kiota_http.middleware.retry_handler import RetryHandler

from pulse.adapters.graph import GraphMailbox, certificate_thumbprint
from pulse.entities.errors import MailboxError
from pulse.entities.mail import Body, OutboundEmail
from tests.emails import make_email
from tests.fakes.graph import MAILBOX, FakeGraph

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK retries without waiting between attempts."""
    monkeypatch.setattr(RetryHandler, "get_delay_time", lambda *args, **kwargs: 0)


def _mailbox(graph: FakeGraph, max_retries: int = 2) -> GraphMailbox:
    return GraphMailbox(graph.client(), MAILBOX, max_retries)


def _throttled(status: int = 429) -> httpx.Response:
    return httpx.Response(status, headers={"Retry-After": "0"})


async def test_every_request_carries_the_token_and_asks_for_immutable_ids() -> None:
    graph = FakeGraph([make_email("m01")])
    mailbox = _mailbox(graph)

    await mailbox.list_inbox()
    await mailbox.move("m01", "Processed")

    for request in graph.requests:
        assert request.headers["Authorization"] == "Bearer token-1"
        assert 'IdType="ImmutableId"' in request.headers["Prefer"]


async def test_listing_asks_for_plain_text_and_the_fields_pulse_reads() -> None:
    graph = FakeGraph()

    await _mailbox(graph).list_inbox()

    [request] = graph.requests
    assert 'outlook.body-content-type="text"' in request.headers["Prefer"]
    assert request.url.params["$select"] == (
        "id,from,sender,subject,receivedDateTime,uniqueBody,body,internetMessageHeaders,"
        "hasAttachments"
    )
    assert request.url.params["$top"] == "50"


async def test_listing_follows_every_page() -> None:
    emails = [
        make_email(f"m0{n}", received=dt.datetime(2026, 9, n, tzinfo=dt.UTC)) for n in range(1, 6)
    ]
    graph = FakeGraph(reversed(emails))

    listed = await _mailbox(graph).list_inbox()

    assert listed == emails
    assert len(graph.requests) == 3


async def test_listing_reads_headers_and_tolerates_missing_parts() -> None:
    graph = FakeGraph([make_email("m01")])
    graph.inbox[0] |= {
        "subject": None,
        "internetMessageHeaders": None,
        "uniqueBody": None,
        "body": None,
    }

    [email] = await _mailbox(graph).list_inbox()

    assert (email.subject, email.headers, email.unique_body, email.body) == ("", {}, "", "")


async def test_listing_tolerates_parts_graph_leaves_out() -> None:
    # Graph omits these fields, rather than sending null, for a message that has none.
    graph = FakeGraph([make_email("m01")])
    for field in ("subject", "internetMessageHeaders", "uniqueBody", "body"):
        del graph.inbox[0][field]

    [email] = await _mailbox(graph).list_inbox()

    assert (email.subject, email.headers, email.unique_body, email.body) == ("", {}, "", "")


async def test_listing_reads_the_new_content_and_the_whole_message() -> None:
    forwarded = make_email(
        "m01", unique_body="Sharing this.", body="Sharing this.\n\nFrom: Litware\nPrices rise."
    )

    [email] = await _mailbox(FakeGraph([forwarded])).list_inbox()

    assert (email.unique_body, email.body) == (forwarded.unique_body, forwarded.body)


async def test_listing_keeps_the_first_of_repeated_headers() -> None:
    graph = FakeGraph([make_email("m01")])
    graph.inbox[0]["internetMessageHeaders"] = [
        {"name": "Auto-Submitted", "value": "auto-replied"},
        {"name": "Auto-Submitted", "value": "no"},
    ]

    [email] = await _mailbox(graph).list_inbox()

    assert email.headers == {"Auto-Submitted": "auto-replied"}


@pytest.mark.parametrize("status", [429, 503])
async def test_throttling_is_retried(status: int) -> None:
    graph = FakeGraph([make_email("m01")])
    graph.failures = [_throttled(status), _throttled(status)]

    assert [e.message_id for e in await _mailbox(graph, max_retries=2).list_inbox()] == ["m01"]


async def test_throttling_beyond_the_retries_fails() -> None:
    graph = FakeGraph()
    graph.failures = [_throttled(), _throttled(), _throttled()]

    with pytest.raises(MailboxError, match="429"):
        await _mailbox(graph, max_retries=2).list_inbox()
    assert len(graph.requests) == 3


async def test_other_errors_fail_without_retrying() -> None:
    graph = FakeGraph()
    graph.failures = [httpx.Response(500, text="private detail")]

    with pytest.raises(MailboxError, match="HTTP 500") as failed:
        await _mailbox(graph).list_inbox()
    assert "private detail" not in str(failed.value)
    assert len(graph.requests) == 1


async def test_connection_errors_fail() -> None:
    graph = FakeGraph()
    graph.failures = [httpx.ConnectError("refused")]

    with pytest.raises(MailboxError, match="ConnectError"):
        await _mailbox(graph).list_inbox()


async def test_unexpected_response_fails() -> None:
    graph = FakeGraph()
    graph.failures = [httpx.Response(200, json={"value": [{"subject": "No sender or ID"}]})]

    with pytest.raises(MailboxError):
        await _mailbox(graph).list_inbox()


async def test_move_creates_the_folder_only_when_missing() -> None:
    graph = FakeGraph([make_email("m01"), make_email("m02")])
    mailbox = _mailbox(graph)

    await mailbox.move("m01", "Processed")
    await mailbox.move("m02", "Processed")

    creates = [
        r for r in graph.requests if r.method == "POST" and r.url.path.endswith("/mailFolders")
    ]
    assert len(creates) == 1
    assert graph.moved == {"Processed": ["m01", "m02"]}


async def test_folder_names_are_quoted_in_the_filter() -> None:
    graph = FakeGraph([make_email("m01")])

    await _mailbox(graph).move("m01", "Pulse's mail")

    assert list(graph.folders) == ["Pulse's mail"]
    assert graph.requests[0].url.params["$filter"] == "displayName eq 'Pulse''s mail'"


async def test_send_creates_the_message_then_sends_it_and_returns_its_id() -> None:
    graph = FakeGraph()
    email = OutboundEmail(
        to=["all-staff@example.org"], subject="Pulse", body=_html("<p>Hi</p>"), reply_to=MAILBOX
    )

    sent_id = await _mailbox(graph).send(email)

    assert [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in graph.requests] == [
        ("POST", "messages"),
        ("POST", "send"),
    ]
    assert sent_id == "draft-1"
    assert graph.sent == [
        {
            "@odata.type": "#microsoft.graph.message",
            "subject": "Pulse",
            "body": {"contentType": "html", "content": "<p>Hi</p>"},
            "toRecipients": [{"emailAddress": {"address": "all-staff@example.org"}}],
            "replyTo": [{"emailAddress": {"address": MAILBOX}}],
        }
    ]


async def test_send_without_reply_to() -> None:
    graph = FakeGraph()
    email = OutboundEmail(
        to=["ops@example.org"], subject="Alert", body=_html("<p>!</p>"), reply_to=None
    )

    await _mailbox(graph).send(email)

    assert "replyTo" not in graph.sent[0]


async def test_reply_goes_in_thread_to_the_named_recipients_only() -> None:
    graph = FakeGraph([make_email("m01")])
    text = Body(content="Version 2 is ready.", content_type="text")

    reply_id = await _mailbox(graph).reply("m01", ["reviewers@example.org"], text)

    assert [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in graph.requests] == [
        ("POST", "createReplyAll"),
        ("PATCH", "reply-1"),
        ("POST", "send"),
    ]
    assert reply_id == "reply-1"
    [reply] = graph.sent
    # The subject is not changed, so Exchange keeps the reply in the thread.
    assert reply == {
        "@odata.type": "#microsoft.graph.message",
        "replyTo": "m01",
        "toRecipients": [{"emailAddress": {"address": "reviewers@example.org"}}],
        "ccRecipients": [],
        "body": {"contentType": "text", "content": "Version 2 is ready."},
    }


async def test_reply_to_a_sent_message_can_be_html() -> None:
    graph = FakeGraph()
    mailbox = _mailbox(graph)
    sent_id = await mailbox.send(
        OutboundEmail(to=[MAILBOX], subject="Draft", body=_html("<p>v1</p>"), reply_to=None)
    )

    await mailbox.reply(sent_id, [MAILBOX], _html("<p>v2</p>"))

    assert graph.sent[1]["body"] == {"contentType": "html", "content": "<p>v2</p>"}


def _html(content: str) -> Body:
    return Body(content=content, content_type="html")


def _certificate(path: Path) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pulse-test")])
    now = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate.fingerprint(hashes.SHA1()).hex().upper()


def test_thumbprint_is_the_certificates_sha1_fingerprint(tmp_path: Path) -> None:
    expected = _certificate(tmp_path / "pulse.pem")

    assert certificate_thumbprint(tmp_path / "pulse.pem") == expected


def test_unreadable_certificate_fails(tmp_path: Path) -> None:
    (tmp_path / "pulse.pem").write_text("not a certificate")

    with pytest.raises(MailboxError, match="certificate"):
        certificate_thumbprint(tmp_path / "pulse.pem")
