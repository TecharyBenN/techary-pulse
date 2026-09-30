"""Callers of Pulse's API and the interface that verifies their bearer tokens."""

from typing import Protocol

from pulse.entities.base import Entity


class Caller(Entity):
    """The holder of a bearer token that passed verification."""

    # The Entra object ID, which stays the same for a person across applications.
    oid: str
    roles: list[str]


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Caller:
        """Return the token's caller; raise InvalidToken when it does not verify."""
        ...
