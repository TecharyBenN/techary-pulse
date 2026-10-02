"""Microsoft Graph adapter: GraphMailbox implements Mailbox with Microsoft's Graph SDK."""

from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from kiota_abstractions.api_error import APIError
from kiota_abstractions.base_request_configuration import RequestConfiguration
from kiota_authentication_azure.azure_identity_authentication_provider import (
    AzureIdentityAuthenticationProvider,
)
from kiota_http.kiota_client_factory import KiotaClientFactory
from kiota_http.middleware.options.retry_handler_option import RetryHandlerOption
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.mail_folder import MailFolder
from msgraph.generated.models.message import Message
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.users.item.mail_folders.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)
from msgraph.generated.users.item.mail_folders.mail_folders_request_builder import (
    MailFoldersRequestBuilder,
)
from msgraph.generated.users.item.messages.item.create_reply_all.create_reply_all_post_request_body import (  # noqa: E501
    CreateReplyAllPostRequestBody,
)
from msgraph.generated.users.item.messages.item.move.move_post_request_body import (
    MovePostRequestBody,
)
from msgraph.graph_request_adapter import GraphRequestAdapter
from msgraph.graph_service_client import GraphServiceClient
from pydantic import ValidationError

from pulse.entities.errors import MailboxError
from pulse.entities.mail import Body, InboundEmail, MessageId, OutboundEmail

# Message IDs stay the same when messages move folders.
_IMMUTABLE_IDS = 'IdType="ImmutableId"'
_PLAIN_TEXT = 'outlook.body-content-type="text"'
_INBOX_FIELDS = [
    "id",
    "from",
    "sender",
    "subject",
    "receivedDateTime",
    "uniqueBody",
    "internetMessageHeaders",
    "hasAttachments",
]
_PAGE_SIZE = 50
_BODY_TYPES = {"html": BodyType.Html, "text": BodyType.Text}


def graph_client(
    credential: AsyncTokenCredential, http_client: httpx.AsyncClient | None = None
) -> GraphServiceClient:
    """A Graph client that signs in with `credential`.

    It is built on Kiota's client factory because msgraph-core's own transport skips the SDK
    middleware, so throttled requests would never be retried.
    """
    client = KiotaClientFactory.create_with_default_middleware(client=http_client)
    auth = AzureIdentityAuthenticationProvider(credential)
    return GraphServiceClient(request_adapter=GraphRequestAdapter(auth, client))


def certificate_thumbprint(path: Path) -> str:
    """The SHA-1 thumbprint Entra ID shows for the certificate in the PEM file."""
    try:
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError) as error:
        raise MailboxError(f"cannot read the Graph certificate: {type(error).__name__}") from error
    return certificate.fingerprint(hashes.SHA1()).hex().upper()


class GraphMailbox:
    """A Pulse mailbox reached through Microsoft Graph."""

    def __init__(self, client: GraphServiceClient, mailbox: str, max_retries: int) -> None:
        self._user = client.users.by_user_id(mailbox)
        self._max_retries = max_retries

    async def list_inbox(self) -> list[InboundEmail]:
        builder = self._user.mail_folders.by_mail_folder_id("inbox").messages
        query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
            select=_INBOX_FIELDS, top=_PAGE_SIZE
        )
        config = self._config(query, _PLAIN_TEXT)
        page = await self._call(builder.get(config))
        messages: list[Message] = []
        while page is not None:
            messages.extend(page.value or [])
            # The next link carries the query itself.
            next_link = page.odata_next_link
            page = (
                await self._call(builder.with_url(next_link).get(self._config(None, _PLAIN_TEXT)))
                if next_link
                else None
            )
        emails = [_inbound(message) for message in messages]
        return sorted(emails, key=lambda email: (email.received, email.message_id))

    async def move(self, message_id: MessageId, folder: str) -> None:
        destination = MovePostRequestBody(destination_id=await self._folder_id(folder))
        message = self._user.messages.by_message_id(message_id)
        await self._call(message.move.post(destination, self._config()))

    async def send(self, email: OutboundEmail) -> MessageId:
        """Create the message, then send it, because sendMail does not return its ID."""
        message = Message(
            subject=email.subject,
            body=_item_body(email.body),
            to_recipients=_recipients(email.to),
            reply_to=_recipients([email.reply_to]) if email.reply_to else None,
        )
        draft = await self._call(self._user.messages.post(message, self._config()))
        return await self._send(_id(draft))

    async def reply(self, message_id: MessageId, to: Sequence[str], body: Body) -> MessageId:
        replying = self._user.messages.by_message_id(message_id).create_reply_all
        reply_id = _id(
            await self._call(replying.post(CreateReplyAllPostRequestBody(), self._config()))
        )
        # The subject stays as Graph sets it: Exchange starts a new conversation when it changes.
        changes = Message(to_recipients=_recipients(to), cc_recipients=[], body=_item_body(body))
        await self._call(self._user.messages.by_message_id(reply_id).patch(changes, self._config()))
        return await self._send(reply_id)

    async def _send(self, draft_id: MessageId) -> MessageId:
        await self._call(self._user.messages.by_message_id(draft_id).send.post(self._config()))
        return draft_id

    async def _folder_id(self, folder: str) -> str:
        """The ID of the top-level folder with this name, created if it is absent."""
        escaped = folder.replace("'", "''")
        query = MailFoldersRequestBuilder.MailFoldersRequestBuilderGetQueryParameters(
            filter=f"displayName eq '{escaped}'"
        )
        found = await self._call(self._user.mail_folders.get(self._config(query)))
        if found is not None and found.value:
            return _id(found.value[0])
        created = MailFolder(display_name=folder)
        return _id(await self._call(self._user.mail_folders.post(created, self._config())))

    def _config(self, query: object = None, prefer: str | None = None) -> RequestConfiguration[Any]:
        """Every request asks for immutable IDs and retries while Graph throttles it."""
        config = RequestConfiguration[Any](
            query_parameters=query, options=[RetryHandlerOption(max_retries=self._max_retries)]
        )
        config.headers.add("Prefer", [_IMMUTABLE_IDS, *([prefer] if prefer else [])])
        return config

    async def _call[T](self, request: Awaitable[T]) -> T:
        try:
            return await request
        except APIError as error:
            raise MailboxError(f"Graph returned HTTP {error.response_status_code}") from error
        except httpx.HTTPError as error:
            raise MailboxError(f"Graph request failed: {type(error).__name__}") from error


def _inbound(message: Message) -> InboundEmail:
    sender = message.from_.email_address if message.from_ else None
    headers: dict[str, str] = {}
    for header in message.internet_message_headers or []:
        if header.name is not None:
            headers.setdefault(header.name, header.value or "")
    try:
        return InboundEmail.model_validate(
            {
                "message_id": message.id,
                "sender_name": sender.name if sender else None,
                "sender_address": sender.address if sender else None,
                "subject": message.subject or "",
                "received": message.received_date_time,
                "has_attachments": message.has_attachments,
                "body": (message.unique_body.content if message.unique_body else None) or "",
                "headers": headers,
            }
        )
    except ValidationError as error:
        raise MailboxError("Graph returned an unexpected message") from error


def _id(item: Message | MailFolder | None) -> str:
    if item is None or item.id is None:
        raise MailboxError("Graph returned no ID")
    return item.id


def _item_body(body: Body) -> ItemBody:
    return ItemBody(content_type=_BODY_TYPES[body.content_type], content=body.content)


def _recipients(addresses: Sequence[str]) -> list[Recipient]:
    return [Recipient(email_address=EmailAddress(address=address)) for address in addresses]
