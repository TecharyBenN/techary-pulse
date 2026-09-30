"""Microsoft Graph: certificate sign-in through MSAL, and the Graph mailbox."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import msal
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from pydantic import AliasPath, AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from pulse.entities.errors import MailboxError
from pulse.entities.mail import InboundEmail, OutboundEmail

GRAPH_URL = "https://graph.microsoft.com/v1.0"
_AUTHORITY = "https://login.microsoftonline.com/{tenant_id}"
_SCOPES = ["https://graph.microsoft.com/.default"]
_IMMUTABLE_IDS = 'IdType="ImmutableId"'
_PLAIN_TEXT = 'outlook.body-content-type="text"'
_INBOX_FIELDS = (
    "id,from,sender,subject,receivedDateTime,uniqueBody,internetMessageHeaders,hasAttachments"
)
_PAGE_SIZE = 50
_THROTTLED = (429, 503)
# Graph sends Retry-After with every throttled response; this covers one that does not.
_DEFAULT_WAIT_SECONDS = 1


class CertificateCredential:
    """Signs in as the Pulse app with the certificate and private key in one PEM file."""

    def __init__(
        self, tenant_id: str, client_id: str, certificate_path: Path, http_client: Any = None
    ) -> None:
        try:
            pem = certificate_path.read_bytes()
            certificate = x509.load_pem_x509_certificate(pem)
        except (OSError, ValueError) as error:
            raise MailboxError(
                f"cannot read the Graph certificate: {type(error).__name__}"
            ) from error
        # Entra ID shows the SHA-1 thumbprint, so an operator can match it to the app registration.
        self.thumbprint = certificate.fingerprint(hashes.SHA1()).hex().upper()
        self._settings: dict[str, Any] = {
            "client_id": client_id,
            "authority": _AUTHORITY.format(tenant_id=tenant_id),
            "client_credential": {"private_key": pem.decode(), "thumbprint": self.thumbprint},
            # MSAL's own HTTP client when None; tests pass a stub.
            "http_client": http_client,
        }
        self._app: Any = None

    async def token(self) -> str:
        """An access token for Graph; MSAL caches it until it nears expiry."""
        result: dict[str, Any] = await asyncio.to_thread(self._acquire)
        if "access_token" not in result:
            # The error description can echo request details, so only the code is kept.
            raise MailboxError(f"Graph sign-in failed: {result.get('error', 'unknown error')}")
        token: str = result["access_token"]
        return token

    def _acquire(self) -> dict[str, Any]:
        # Creating the app fetches the tenant's metadata, so it happens here, off the event loop.
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(**self._settings)
        result: dict[str, Any] = self._app.acquire_token_for_client(scopes=_SCOPES)
        return result


class _Message(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    sender_name: str = Field(validation_alias=AliasPath("from", "emailAddress", "name"))
    sender_address: str = Field(validation_alias=AliasPath("from", "emailAddress", "address"))
    subject: str | None
    received_date_time: AwareDatetime
    unique_body: dict[str, Any] | None
    internet_message_headers: list[dict[str, str]] | None
    has_attachments: bool

    def email(self) -> InboundEmail:
        headers: dict[str, str] = {}
        for header in self.internet_message_headers or []:
            # A repeated header keeps its first value, as header_value would read it.
            headers.setdefault(header["name"], header["value"])
        return InboundEmail(
            message_id=self.id,
            sender_name=self.sender_name,
            sender_address=self.sender_address,
            subject=self.subject or "",
            received=self.received_date_time,
            has_attachments=self.has_attachments,
            body=(self.unique_body or {}).get("content") or "",
            headers=headers,
        )


class _Page(BaseModel):
    value: list[_Message]
    next_link: str | None = Field(default=None, validation_alias="@odata.nextLink")


class _Created(BaseModel):
    id: str


class _Folders(BaseModel):
    value: list[_Created]


class GraphMailbox:
    """One Pulse mailbox, reached through Graph as the Pulse app."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: Callable[[], Awaitable[str]],
        mailbox: str,
        max_retries: int,
    ) -> None:
        self._client = client
        self._token = token
        self._base = f"/users/{quote(mailbox, safe='@')}"
        self._max_retries = max_retries

    async def list_inbox(self) -> list[InboundEmail]:
        url: str | None = f"{self._base}/mailFolders/inbox/messages"
        params: dict[str, str] | None = {"$select": _INBOX_FIELDS, "$top": str(_PAGE_SIZE)}
        emails: list[InboundEmail] = []
        while url is not None:
            response = await self._request("GET", url, params=params, prefer=_PLAIN_TEXT)
            page = _parse(_Page, response)
            emails.extend(message.email() for message in page.value)
            # The next link carries the query itself.
            url, params = page.next_link, None
        return sorted(emails, key=lambda email: (email.received, email.message_id))

    async def move(self, message_id: str, folder: str) -> None:
        folder_id = await self._folder_id(folder)
        await self._request(
            "POST", f"{self._message(message_id)}/move", json={"destinationId": folder_id}
        )

    async def send(self, email: OutboundEmail) -> None:
        message: dict[str, Any] = {
            "subject": email.subject,
            "body": {"contentType": "HTML", "content": email.html},
            "toRecipients": _recipients(email.to),
        }
        if email.reply_to is not None:
            message["replyTo"] = _recipients([email.reply_to])
        await self._request("POST", f"{self._base}/sendMail", json={"message": message})

    async def reply(self, message_id: str, to: Sequence[str], text: str) -> None:
        response = await self._request("POST", f"{self._message(message_id)}/createReplyAll")
        reply = self._message(_parse(_Created, response).id)
        await self._request(
            "PATCH",
            reply,
            json={
                "toRecipients": _recipients(to),
                "ccRecipients": [],
                "body": {"contentType": "Text", "content": text},
            },
        )
        await self._request("POST", f"{reply}/send")

    async def _folder_id(self, folder: str) -> str:
        """The ID of the top-level folder with this name, creating the folder if it is absent."""
        escaped = folder.replace("'", "''")
        params = {"$filter": f"displayName eq '{escaped}'"}
        response = await self._request("GET", f"{self._base}/mailFolders", params=params)
        found = _parse(_Folders, response).value
        if found:
            return found[0].id
        response = await self._request(
            "POST", f"{self._base}/mailFolders", json={"displayName": folder}
        )
        return _parse(_Created, response).id

    def _message(self, message_id: str) -> str:
        return f"{self._base}/messages/{quote(message_id, safe='')}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        """Send the request, waiting and retrying while Graph throttles it."""
        preferences = ", ".join(p for p in (_IMMUTABLE_IDS, prefer) if p)
        for attempt in range(self._max_retries + 1):
            headers = {"Authorization": f"Bearer {await self._token()}", "Prefer": preferences}
            try:
                response = await self._client.request(
                    method, url, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as error:
                raise MailboxError(f"Graph request failed: {type(error).__name__}") from error
            if response.status_code not in _THROTTLED or attempt == self._max_retries:
                break
            await asyncio.sleep(_retry_after(response))
        if response.is_error:
            # Graph's error body can quote message content, so only the status is kept.
            raise MailboxError(f"Graph returned HTTP {response.status_code}")
        return response


def _parse[T: BaseModel](model: type[T], response: httpx.Response) -> T:
    try:
        return model.model_validate_json(response.content)
    except ValidationError as error:
        raise MailboxError(f"Graph returned an unexpected {model.__name__}") from error


def _recipients(addresses: Sequence[str]) -> list[dict[str, dict[str, str]]]:
    return [{"emailAddress": {"address": address}} for address in addresses]


def _retry_after(response: httpx.Response) -> float:
    try:
        return float(response.headers.get("Retry-After", _DEFAULT_WAIT_SECONDS))
    except ValueError:
        return _DEFAULT_WAIT_SECONDS
