from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pulse.entities.mail import Body, InboundEmail, OutboundEmail


@dataclass(frozen=True)
class Reply:
    message_id: str
    to: tuple[str, ...]
    body: Body
    reply_id: str


class FakeMailbox:
    """An in-memory mailbox that records what Pulse did to it."""

    def __init__(self, inbox: Iterable[InboundEmail] = ()) -> None:
        self.inbox = list(inbox)
        self.folders: dict[str, list[InboundEmail]] = {}
        self.sent: list[OutboundEmail] = []
        self.replies: list[Reply] = []

    async def list_inbox(self) -> list[InboundEmail]:
        return sorted(self.inbox, key=lambda email: (email.received, email.message_id))

    async def move(self, message_id: str, folder: str) -> None:
        [email] = [email for email in self.inbox if email.message_id == message_id]
        self.inbox.remove(email)
        self.folders.setdefault(folder, []).append(email)

    async def send(self, email: OutboundEmail) -> str:
        self.sent.append(email)
        return f"sent-{len(self.sent)}"

    async def reply(self, message_id: str, to: Sequence[str], body: Body) -> str:
        reply_id = f"reply-{len(self.replies) + 1}"
        self.replies.append(Reply(message_id, tuple(to), body, reply_id))
        return reply_id
