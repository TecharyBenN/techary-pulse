from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pulse.config import Config, load_config
from pulse.errors import GraphError
from pulse.mail import GraphMailbox, Mailbox, load_certificate
from pulse.models import OutgoingEmail

from ..support import FakeGraph, FakeMailbox, message

pytestmark = pytest.mark.anyio

MESSAGES = [
    message("m1", headers={"Auto-Submitted": "no", "msip_labels": "MSIP_Label_x_Enabled=true"}),
    message("m2", has_attachments=True),
    message("m3", body="Third update with enough text."),
]
EMAIL = OutgoingEmail(to=["r@techary.ai"], reply_to=["r@techary.ai"], subject="S", html="<p>x</p>")
EMAIL_NO_REPLY_TO = OutgoingEmail(to=["r@techary.ai"], subject="S", html="<p>x</p>")


@pytest.fixture
def config(config_dir: Path) -> Config:
    return load_config(config_dir / "config.example.yaml")


def graph_mailbox(
    config: Config, graph: FakeGraph, sleeps: list[float] | None = None
) -> GraphMailbox:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return GraphMailbox(
        config.graph,
        config.mailboxes.submissions,
        token=lambda: "token",
        http=graph.client(),
        sleep=sleep,
    )


# Interface tests: both mailboxes behave the same through the Mailbox interface.


@pytest.fixture(params=["fake", "graph"])
def mailbox(request: pytest.FixtureRequest, config: Config) -> Iterator[Mailbox]:
    if request.param == "fake":
        yield FakeMailbox(MESSAGES)
    else:
        yield graph_mailbox(config, FakeGraph(MESSAGES))


async def test_lists_every_inbox_message(mailbox: Mailbox) -> None:
    listed = await mailbox.list_inbox()
    assert [m.id for m in listed] == ["m1", "m2", "m3"]
    assert listed[1].has_attachments
    assert listed[0].sender_address == "priya.shah@techary.ai"


async def test_moved_messages_leave_the_inbox_and_can_be_moved_again(mailbox: Mailbox) -> None:
    await mailbox.move("m1", "Processed")
    await mailbox.move("m2", "Rejected")
    await mailbox.move("m1", "Processed")
    assert [m.id for m in await mailbox.list_inbox()] == ["m3"]


async def test_send_is_accepted(mailbox: Mailbox) -> None:
    await mailbox.send(EMAIL)


async def test_reply_is_accepted(mailbox: Mailbox) -> None:
    await mailbox.reply("m1", EMAIL.to, EMAIL.html)


# Graph specifics.


async def test_every_request_asks_for_immutable_ids_and_lists_ask_for_text(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    box = graph_mailbox(config, graph)
    await box.list_inbox()
    await box.move("m1", "Processed")
    assert all('IdType="ImmutableId"' in r.headers["Prefer"] for r in graph.requests)
    listing = [r for r in graph.requests if r.url.path.endswith("/inbox/messages")]
    assert all('outlook.body-content-type="text"' in r.headers["Prefer"] for r in listing)
    assert "$select=" in str(listing[0].url) and "hasAttachments" in str(listing[0].url)


async def test_listing_follows_next_link_and_lowercases_headers(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    listed = await graph_mailbox(config, graph).list_inbox()
    assert len([r for r in graph.requests if r.url.path.endswith("/inbox/messages")]) == 2
    assert listed[0].headers == {"auto-submitted": "no", "msip_labels": "MSIP_Label_x_Enabled=true"}


async def test_missing_folder_is_created_once(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    box = graph_mailbox(config, graph)
    await box.move("m1", "Processed")
    await box.move("m2", "Processed")
    creates = [
        r for r in graph.requests if r.method == "POST" and r.url.path.endswith("/mailFolders")
    ]
    assert len(creates) == 1
    assert graph.folders["Processed"]["messages"] == ["m1", "m2"]


async def test_existing_folder_is_reused(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    graph.folders["Processed"] = {"id": "existing", "messages": []}
    await graph_mailbox(config, graph).move("m1", "Processed")
    assert graph.folders["Processed"]["messages"] == ["m1"]


async def test_send_sets_recipients_reply_to_and_html(config: Config) -> None:
    graph = FakeGraph([])
    await graph_mailbox(config, graph).send(EMAIL)
    sent = graph.sent[0]
    assert sent["toRecipients"] == [{"emailAddress": {"address": "r@techary.ai"}}]
    assert sent["replyTo"] == [{"emailAddress": {"address": "r@techary.ai"}}]
    assert sent["body"] == {"contentType": "HTML", "content": "<p>x</p>"}


async def test_send_omits_reply_to_when_empty(config: Config) -> None:
    graph = FakeGraph([])
    await graph_mailbox(config, graph).send(EMAIL_NO_REPLY_TO)
    assert "replyTo" not in graph.sent[0]


async def test_reply_replaces_recipients_and_sets_the_html_body(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    await graph_mailbox(config, graph).reply("m1", EMAIL.to, EMAIL.html)
    reply = graph.replies[0]
    assert reply["source_message_id"] == "m1"
    assert reply["toRecipients"] == [{"emailAddress": {"address": "r@techary.ai"}}]
    assert reply["ccRecipients"] == []
    assert reply["body"] == {"contentType": "HTML", "content": "<p>x</p>"}


async def test_reply_requests_create_reply_patch_and_send_in_order(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    await graph_mailbox(config, graph).reply("m1", EMAIL.to, EMAIL.html)
    methods = [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in graph.requests]
    assert methods == [
        ("POST", "createReplyAll"),
        ("PATCH", "reply-m1"),
        ("POST", "send"),
    ]
    assert all('IdType="ImmutableId"' in r.headers["Prefer"] for r in graph.requests)


async def test_throttling_waits_for_retry_after_then_succeeds(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    graph.injected = [httpx.Response(429, headers={"Retry-After": "7"})] * 2
    sleeps: list[float] = []
    assert len(await graph_mailbox(config, graph, sleeps).list_inbox()) == 3
    assert sleeps == [7.0, 7.0]


async def test_throttling_beyond_max_retries_fails(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    graph.injected = [httpx.Response(429, headers={"Retry-After": "1"})] * 10
    with pytest.raises(GraphError, match="429"):
        await graph_mailbox(config, graph).list_inbox()
    assert len(graph.requests) == config.graph.max_retries + 1


async def test_server_error_fails_without_retrying(config: Config) -> None:
    graph = FakeGraph(MESSAGES)
    graph.injected = [httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})]
    with pytest.raises(GraphError, match="503 ServiceUnavailable"):
        await graph_mailbox(config, graph).list_inbox()
    assert len(graph.requests) == 1


def test_certificate_thumbprint_is_calculated_from_the_file(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "pulse.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + cert.public_bytes(serialization.Encoding.PEM)
    )
    key_pem, thumbprint = load_certificate(path)
    assert thumbprint == cert.fingerprint(hashes.SHA1()).hex().upper()
    assert key_pem.startswith("-----BEGIN PRIVATE KEY-----")
