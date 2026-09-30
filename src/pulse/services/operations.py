"""The orchestrator's actions: each changes the newsletter's state and saves it."""

import uuid
from collections.abc import Callable

from pulse.entities.base import Entity
from pulse.entities.clock import Clock
from pulse.entities.lifecycle import open_newsletter, update
from pulse.entities.mail import InboundEmail, Mailbox, ScreenedEmail
from pulse.entities.store import Store


class StartResult(Entity):
    newsletter_id: str
    opened: bool
    added: int
    rejected: int


class Operations:
    def __init__(
        self,
        store: Store,
        submissions_mailbox: Mailbox,
        screen: Callable[[InboundEmail], ScreenedEmail],
        clock: Clock,
    ) -> None:
        self._store = store
        self._mailbox = submissions_mailbox
        self._screen = screen
        self._clock = clock

    async def start_newsletter(self) -> StartResult:
        """Open a newsletter with every pending email, or add those new to the open one."""
        now = self._clock.now()
        newsletter = await self._store.get_open_newsletter()
        opened = newsletter is None
        known: set[str] = set()
        if newsletter is None:
            newsletter = open_newsletter(str(uuid.uuid4()), now)
        else:
            newsletter = update(newsletter, now)
            emails = await self._store.list_screened_emails(newsletter.newsletter_id)
            known = {e.message_id for e in emails}
        inbox = await self._mailbox.list_inbox()
        added = [self._screen(email) for email in inbox if email.message_id not in known]
        await self._store.save_start(newsletter, added)
        return StartResult(
            newsletter_id=newsletter.newsletter_id,
            opened=opened,
            added=len(added),
            rejected=sum(1 for s in added if s.rejection is not None),
        )
