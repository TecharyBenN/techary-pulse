"""Connections to the dev tenant for `live` tests, using ./config.yaml and the certificate it
names."""

import asyncio
import contextlib
import functools
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from azure.identity.aio import CertificateCredential
from kiota_abstractions.base_request_configuration import RequestConfiguration
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.file_attachment import FileAttachment
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.message import Message
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.models.single_value_legacy_extended_property import (
    SingleValueLegacyExtendedProperty,
)
from msgraph.generated.users.item.mail_folders.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)
from msgraph.generated.users.item.messages.item.message_item_request_builder import (
    MessageItemRequestBuilder,
)
from msgraph.graph_service_client import GraphServiceClient

from pulse.adapters.graph import GraphMailbox, graph_client
from pulse.adapters.render import Renderer
from pulse.agents.orchestrator.run import Orchestrator
from pulse.config import Config
from pulse.entities.mail import InboundEmail, Mailbox, address_in
from pulse.entities.store import Store
from pulse.main import build_operations, build_orchestrator, build_outbox, build_screen
from tests.emails import STAFF_DOMAIN, seedable_corpus_messages

# Sent Items can lag behind a send, so a sent message is looked for this long.
_SENT_WAIT_SECONDS = 120

# PR_MESSAGE_FLAGS set to read, so a created message is received mail, not an unsent draft.
_RECEIVED = SingleValueLegacyExtendedProperty(id="Integer 0x0E07", value="1")


@contextlib.asynccontextmanager
async def live_graph(config: Config) -> AsyncIterator[GraphServiceClient]:
    """A Graph client signed in as the Pulse app with the configured certificate."""
    graph = config.graph
    credential = CertificateCredential(
        graph.tenant_id, graph.client_id, certificate_path=str(graph.certificate_path)
    )
    async with credential:
        yield graph_client(credential)


def graph_mailbox(config: Config, client: GraphServiceClient, address: str) -> GraphMailbox:
    return GraphMailbox(client, address, config.graph.max_retries)


def check_seeded_inbox(config: Config, inbox: list[InboundEmail], extra: int = 0) -> None:
    """The inbox holds the seedable corpus once, and `extra` other messages that pass the
    pre-filter. Others it rejects, such as a mail filter's reports, are never extracted, so
    they are ignored."""
    corpus = {m["subject"] for m in seedable_corpus_messages()}
    screen = build_screen(config)
    seeded = sorted(email.subject for email in inbox if email.subject in corpus)
    others = [e for e in inbox if e.subject not in corpus and screen(e).rejection is None]
    assert seeded == sorted(corpus) and len(others) == extra, (
        "reset the dev inbox and seed the corpus once"
    )


def live_orchestrator(
    config: Config,
    store: Store,
    submissions: Mailbox,
    conversation: Mailbox,
    clock: Callable[[], datetime] | None = None,
    lock: asyncio.Lock | None = None,
) -> Orchestrator:
    """The orchestrator as main builds it, with the gateway key from the environment."""
    renderer = Renderer(config.timezone)
    outbox = build_outbox(config, conversation, store, renderer)
    now = clock or functools.partial(datetime.now, UTC)
    operations = build_operations(config, store, submissions, outbox, renderer, now)
    llm_key = os.environ[config.llm.api_key_env]
    return build_orchestrator(config, store, operations, llm_key, lock or asyncio.Lock())


async def conversation_id(client: GraphServiceClient, address: str, message_id: str) -> str:
    """The Exchange conversation a message belongs to, which every reply in a thread shares."""
    query = MessageItemRequestBuilder.MessageItemRequestBuilderGetQueryParameters(
        select=["conversationId"]
    )
    config = RequestConfiguration[
        MessageItemRequestBuilder.MessageItemRequestBuilderGetQueryParameters
    ](query_parameters=query)
    config.headers.add("Prefer", 'IdType="ImmutableId"')
    message = await client.users.by_user_id(address).messages.by_message_id(message_id).get(config)
    assert message is not None and message.conversation_id is not None
    return message.conversation_id


async def place_in_inbox(
    config: Config, client: GraphServiceClient, address: str, message: dict[str, Any]
) -> None:
    """Create a corpus message in the mailbox's inbox as received mail from its own sender.

    Staff senders take the first configured sender domain, so the corpus suits any tenant;
    outside senders keep theirs.
    """
    local, _, domain = message["sender_address"].rpartition("@")
    if domain == STAFF_DOMAIN:
        domain = config.allowed_sender_domains[0]
    sender = Recipient(
        email_address=EmailAddress(name=message["sender_name"], address=f"{local}@{domain}")
    )
    created = Message(
        subject=message["subject"],
        from_=sender,
        sender=sender,
        to_recipients=[Recipient(email_address=EmailAddress(address=address))],
        body=ItemBody(content_type=BodyType.Text, content=message["body"]),
        single_value_extended_properties=[_RECEIVED],
    )
    if message.get("has_attachments"):
        created.attachments = [
            FileAttachment(
                name="notes.txt", content_bytes=b"Synthetic attachment for the test corpus."
            )
        ]
    inbox = client.users.by_user_id(address).mail_folders.by_mail_folder_id("inbox")
    await inbox.messages.post(created)


async def sent_to(client: GraphServiceClient, mailbox: str, recipient: str, since: datetime) -> str:
    """The body of the newest message the mailbox sent to `recipient` at or after `since`."""
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        select=["toRecipients", "sentDateTime", "body"], orderby=["sentDateTime desc"], top=10
    )
    config = RequestConfiguration[MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters](
        query_parameters=query
    )
    sent_items = client.users.by_user_id(mailbox).mail_folders.by_mail_folder_id("sentitems")
    # Graph gives sent times to the second.
    since = since.replace(microsecond=0)
    async with asyncio.timeout(_SENT_WAIT_SECONDS):
        while True:
            page = await sent_items.messages.get(config)
            for message in page.value if page and page.value else []:
                to = [
                    r.email_address.address or ""
                    for r in message.to_recipients or []
                    if r.email_address
                ]
                sent = message.sent_date_time
                if (
                    address_in(recipient, to)
                    and sent is not None
                    and sent >= since
                    and message.body
                ):
                    return message.body.content or ""
            await asyncio.sleep(5)
