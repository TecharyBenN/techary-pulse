import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pulse.adapters.graph import CertificateCredential, GraphMailbox
from pulse.entities.errors import MailboxError
from pulse.entities.mail import OutboundEmail
from tests.emails import make_email
from tests.fakes.graph import MAILBOX, FakeGraph

pytestmark = pytest.mark.anyio


async def _token() -> str:
    return "token-1"


def _mailbox(graph: FakeGraph, max_retries: int = 2) -> GraphMailbox:
    return GraphMailbox(graph.client(), _token, MAILBOX, max_retries)


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
        "id,from,sender,subject,receivedDateTime,uniqueBody,internetMessageHeaders,hasAttachments"
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
    graph.inbox[0] |= {"subject": None, "internetMessageHeaders": None, "uniqueBody": None}

    [email] = await _mailbox(graph).list_inbox()

    assert (email.subject, email.headers, email.body) == ("", {}, "")


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
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="https://graph")

    with pytest.raises(MailboxError, match="ConnectError"):
        await GraphMailbox(client, _token, MAILBOX, 2).list_inbox()


async def test_unexpected_response_fails() -> None:
    graph = FakeGraph()
    graph.failures = [httpx.Response(200, json={"unexpected": True})]

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


async def test_send_is_html_with_its_recipients_and_reply_to() -> None:
    graph = FakeGraph()
    email = OutboundEmail(
        to=["all-staff@techary.ai"], subject="Pulse", html="<p>Hi</p>", reply_to=MAILBOX
    )

    await _mailbox(graph).send(email)

    assert graph.sent == [
        {
            "subject": "Pulse",
            "body": {"contentType": "HTML", "content": "<p>Hi</p>"},
            "toRecipients": [{"emailAddress": {"address": "all-staff@techary.ai"}}],
            "replyTo": [{"emailAddress": {"address": MAILBOX}}],
        }
    ]


async def test_send_without_reply_to() -> None:
    graph = FakeGraph()
    email = OutboundEmail(to=["ops@techary.ai"], subject="Alert", html="<p>!</p>", reply_to=None)

    await _mailbox(graph).send(email)

    assert "replyTo" not in graph.sent[0]


async def test_reply_goes_in_thread_to_the_named_recipients_only() -> None:
    graph = FakeGraph([make_email("m01")])

    await _mailbox(graph).reply("m01", ["reviewers@techary.ai"], "Version 2 is ready.")

    assert [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in graph.requests] == [
        ("POST", "createReplyAll"),
        ("PATCH", "reply-1"),
        ("POST", "send"),
    ]
    [reply] = graph.sent
    assert reply == {
        "replyTo": "m01",
        "toRecipients": [{"emailAddress": {"address": "reviewers@techary.ai"}}],
        "ccRecipients": [],
        "body": {"contentType": "Text", "content": "Version 2 is ready."},
    }


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


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self.text = json.dumps(body)
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        """Every stub response succeeds."""


class _EntraStub:
    """Stands in for MSAL's HTTP client: the tenant's metadata, then the token response.

    MSAL reads the tenant's OpenID configuration, and after an error its instance metadata, so
    every GET answers with both.
    """

    def __init__(self, token: dict[str, Any]) -> None:
        self.token = token
        self.posts: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        base = "https://login.microsoftonline.com/tenant-1"
        return _Response(
            {
                "token_endpoint": f"{base}/oauth2/v2.0/token",
                "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
                "issuer": f"{base}/v2.0",
                "metadata": [],
            }
        )

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append(kwargs.get("data", {}))
        return _Response(self.token)

    def close(self) -> None:
        """Nothing to close."""


def test_credential_calculates_the_thumbprint(tmp_path: Path) -> None:
    expected = _certificate(tmp_path / "pulse.pem")

    credential = CertificateCredential("tenant-1", "client-1", tmp_path / "pulse.pem")

    assert credential.thumbprint == expected


def test_unreadable_certificate_fails(tmp_path: Path) -> None:
    (tmp_path / "pulse.pem").write_text("not a certificate")

    with pytest.raises(MailboxError, match="certificate"):
        CertificateCredential("tenant-1", "client-1", tmp_path / "pulse.pem")


async def test_credential_signs_in_with_a_client_assertion(tmp_path: Path) -> None:
    _certificate(tmp_path / "pulse.pem")
    stub = _EntraStub({"access_token": "token-9", "token_type": "Bearer", "expires_in": 3600})
    credential = CertificateCredential("tenant-1", "client-1", tmp_path / "pulse.pem", stub)

    assert await credential.token() == "token-9"
    [post] = stub.posts
    assert post["scope"] == "https://graph.microsoft.com/.default"
    assert "client_assertion" in post


async def test_failed_sign_in_raises_without_the_description(tmp_path: Path) -> None:
    _certificate(tmp_path / "pulse.pem")
    stub = _EntraStub({"error": "invalid_client", "error_description": "private detail"})
    credential = CertificateCredential("tenant-1", "client-1", tmp_path / "pulse.pem", stub)

    with pytest.raises(MailboxError, match="invalid_client") as failed:
        await credential.token()
    assert "private detail" not in str(failed.value)
