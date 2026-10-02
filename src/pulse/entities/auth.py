"""Callers of the chat endpoint, as their verified bearer tokens identify them."""

from pulse.entities.base import Entity


class Caller(Entity):
    """The holder of a bearer token that passed verification."""

    # The Entra object ID, which stays the same for a person across applications.
    oid: str
    roles: list[str]
