"""The mailbox interface the pipeline uses."""

from typing import Protocol

from pulse.models import Message, OutgoingEmail


class Mailbox(Protocol):
    """What the pipeline needs from the Pulse mailbox."""

    def list_inbox(self) -> list[Message]:
        """Return every message in the inbox."""
        ...

    def move(self, message_id: str, folder: str) -> None:
        """Move a message to the named folder, creating the folder if it is absent."""
        ...

    def send(self, email: OutgoingEmail) -> None:
        """Send an email from the Pulse mailbox."""
        ...
