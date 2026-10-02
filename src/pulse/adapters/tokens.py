"""Bearer token verification: JwtVerifier checks a token against the issuer's key set."""

import asyncio
from pathlib import Path
from typing import Any

import jwt
from pydantic import ValidationError

from pulse.entities.auth import Caller
from pulse.entities.errors import InvalidToken

# Entra ID signs with RS256; accepting only it rules out algorithm confusion.
_ALGORITHMS = ["RS256"]
_REQUIRED_CLAIMS = ["iss", "aud", "exp", "oid"]


class JwtVerifier:
    """Verifies RS256 tokens from one issuer, for one audience.

    `jwks` is the issuer's key set, as a URL, which is fetched and cached, or as a file path,
    which is read on every verification.
    """

    def __init__(self, issuer: str, audience: str, jwks: str) -> None:
        self._issuer = issuer
        self._audience = audience
        self._client = jwt.PyJWKClient(jwks) if "://" in jwks else None
        self._path = Path(jwks)

    async def verify(self, token: str) -> Caller:
        """The token's caller; raises InvalidToken when the token does not verify."""
        return await asyncio.to_thread(self._verify, token)

    def _verify(self, token: str) -> Caller:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._signing_key(token),
                algorithms=_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": _REQUIRED_CLAIMS},
            )
            return Caller.model_validate({"oid": claims["oid"], "roles": claims.get("roles", [])})
        except (jwt.PyJWTError, OSError, KeyError, ValidationError) as error:
            raise InvalidToken(f"the token did not verify: {type(error).__name__}") from error

    def _signing_key(self, token: str) -> Any:
        if self._client is not None:
            return self._client.get_signing_key_from_jwt(token).key
        # PyJWKClient reads only http and https URLs.
        key_set = jwt.PyJWKSet.from_json(self._path.read_text(encoding="utf-8"))
        return key_set[jwt.get_unverified_header(token).get("kid", "")].key
