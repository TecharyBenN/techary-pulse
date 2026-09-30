"""Bearer token verification against the issuer's JSON Web Key Set."""

import asyncio
from pathlib import Path
from typing import Any

import jwt
from pydantic import ValidationError

from pulse.entities.auth import Caller
from pulse.entities.clock import Clock
from pulse.entities.errors import InvalidToken

# Entra ID signs with RS256; accepting only it rules out algorithm confusion.
_ALGORITHMS = ["RS256"]
_REQUIRED_CLAIMS = ["iss", "aud", "exp", "oid"]


class JwtVerifier:
    """Verifies RS256 tokens from one issuer, for one audience.

    `jwks` is the issuer's key set, as a URL or a file path. A URL is fetched and cached; a file
    is read on every verification, so a rotated key takes effect without a restart.
    """

    def __init__(self, issuer: str, audience: str, jwks: str, clock: Clock) -> None:
        self._issuer = issuer
        self._audience = audience
        self._clock = clock
        self._client = jwt.PyJWKClient(jwks) if "://" in jwks else None
        self._path = Path(jwks)

    async def verify(self, token: str) -> Caller:
        return await asyncio.to_thread(self._verify, token)

    def _verify(self, token: str) -> Caller:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._signing_key(token),
                algorithms=_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                # Times are checked below, against Pulse's clock.
                options={
                    "require": _REQUIRED_CLAIMS,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
            self._check_times(claims)
            return Caller.model_validate({"oid": claims["oid"], "roles": claims.get("roles", [])})
        except (jwt.PyJWTError, OSError, ValidationError) as error:
            raise InvalidToken(f"the token did not verify: {type(error).__name__}") from error

    def _signing_key(self, token: str) -> Any:
        if self._client is not None:
            return self._client.get_signing_key_from_jwt(token).key
        kid = jwt.get_unverified_header(token).get("kid")
        key_set = jwt.PyJWKSet.from_json(self._path.read_text(encoding="utf-8"))
        for key in key_set.keys:
            if key.key_id == kid:
                return key.key
        raise InvalidToken("no key in the key set matches the token")

    def _check_times(self, claims: dict[str, Any]) -> None:
        now = self._clock.now().timestamp()
        expiry, start = claims["exp"], claims.get("nbf")
        if not isinstance(expiry, int | float) or now >= expiry:
            raise InvalidToken("the token has expired")
        if start is not None and (not isinstance(start, int | float) or now < start):
            raise InvalidToken("the token is not valid yet")
