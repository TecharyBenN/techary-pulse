"""The mailbox interface the pipeline uses, and its Microsoft Graph implementation."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import msal
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from pulse.config import GraphConfig
from pulse.errors import GraphError
from pulse.models import Message, OutgoingEmail

log = logging.getLogger("pulse.mail")

GRAPH = "https://graph.microsoft.com/v1.0"
_SELECT = "id,from,sender,subject,receivedDateTime,uniqueBody,internetMessageHeaders,hasAttachments"


class Mailbox(Protocol):
    """What Pulse needs from a mailbox."""

    async def list_inbox(self) -> list[Message]:
        """Return every message in the inbox."""
        ...

    async def move(self, message_id: str, folder: str) -> None:
        """Move a message to the named folder, creating the folder if it is absent."""
        ...

    async def send(self, email: OutgoingEmail) -> None:
        """Send an email from the mailbox."""
        ...


def load_certificate(path: Path) -> tuple[str, str]:
    """Return the private key as PEM and the certificate's SHA-1 thumbprint from ``path``.

    The file holds the private key and the certificate, as the design requires.
    """
    data = path.read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    certificate = x509.load_pem_x509_certificate(data)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key_pem, certificate.fingerprint(hashes.SHA1()).hex().upper()


def msal_token(graph: GraphConfig) -> Callable[[], str]:
    """Return a function that gets a Graph token with the certificate, through MSAL."""
    key_pem, thumbprint = load_certificate(graph.certificate_path)
    log.info("certificate loaded", extra={"thumbprint": thumbprint})
    try:
        app = msal.ConfidentialClientApplication(
            graph.client_id,
            authority=f"https://login.microsoftonline.com/{graph.tenant_id}",
            client_credential={"private_key": key_pem, "thumbprint": thumbprint},
        )
    except ValueError as exc:
        # MSAL raises ValueError when Entra ID rejects the tenant, for example a mistyped ID.
        raise GraphError(f"Entra ID rejected tenant {graph.tenant_id}") from exc

    def token() -> str:
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise GraphError(f"token request failed: {result.get('error')}")
        return str(result["access_token"])

    return token


def _message(raw: dict[str, Any]) -> Message:
    sender = (raw.get("from") or {}).get("emailAddress") or {}
    headers = {h["name"].lower(): h["value"] for h in raw.get("internetMessageHeaders") or []}
    return Message(
        id=raw["id"],
        sender_name=sender.get("name", ""),
        sender_address=sender.get("address", ""),
        subject=raw.get("subject") or "",
        received_at=datetime.fromisoformat(raw["receivedDateTime"]),
        body=(raw.get("uniqueBody") or {}).get("content", ""),
        headers=headers,
        has_attachments=bool(raw.get("hasAttachments")),
    )


class GraphMailbox:
    """The Pulse mailbox, through Microsoft Graph."""

    def __init__(
        self,
        graph: GraphConfig,
        address: str,
        token: Callable[[], str],
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._max_retries = graph.max_retries
        self._user = f"{GRAPH}/users/{address}"
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=60)
        self._sleep = sleep
        self._folders: dict[str, str] = {}

    async def _request(
        self, method: str, url: str, prefer_text: bool = False, **kwargs: Any
    ) -> httpx.Response:
        prefer = 'IdType="ImmutableId"'
        if prefer_text:
            prefer += ', outlook.body-content-type="text"'
        for attempt in range(self._max_retries + 1):
            token = await asyncio.to_thread(self._token)
            headers = {"Authorization": f"Bearer {token}", "Prefer": prefer}
            response = await self._http.request(method, url, headers=headers, **kwargs)
            if response.status_code == 429 and attempt < self._max_retries:
                await self._sleep(float(response.headers.get("Retry-After", "1")))
                continue
            if response.is_error:
                code = response.json().get("error", {}).get("code", "") if response.content else ""
                raise GraphError(
                    f"{method} {url.removeprefix(GRAPH)}: {response.status_code} {code}"
                )
            return response
        raise AssertionError("unreachable")

    async def list_inbox(self) -> list[Message]:
        url: str | None = f"{self._user}/mailFolders/inbox/messages?$select={_SELECT}&$top=50"
        messages: list[Message] = []
        while url:
            page = (await self._request("GET", url, prefer_text=True)).json()
            messages.extend(_message(raw) for raw in page.get("value", []))
            url = page.get("@odata.nextLink")
        return sorted(messages, key=lambda m: m.received_at)

    async def _folder_id(self, name: str) -> str:
        if name not in self._folders:
            found = (
                await self._request(
                    "GET",
                    f"{self._user}/mailFolders",
                    params={"$filter": f"displayName eq '{name}'"},
                )
            ).json()["value"]
            if found:
                self._folders[name] = found[0]["id"]
            else:
                created = await self._request(
                    "POST", f"{self._user}/mailFolders", json={"displayName": name}
                )
                self._folders[name] = created.json()["id"]
        return self._folders[name]

    async def move(self, message_id: str, folder: str) -> None:
        destination = await self._folder_id(folder)
        await self._request(
            "POST", f"{self._user}/messages/{message_id}/move", json={"destinationId": destination}
        )

    async def send(self, email: OutgoingEmail) -> None:
        def recipients(addresses: list[str]) -> list[dict[str, Any]]:
            return [{"emailAddress": {"address": a}} for a in addresses]

        message: dict[str, Any] = {
            "subject": email.subject,
            "body": {"contentType": "HTML", "content": email.html},
            "toRecipients": recipients(email.to),
        }
        if email.reply_to:
            message["replyTo"] = recipients(email.reply_to)
        await self._request("POST", f"{self._user}/sendMail", json={"message": message})
