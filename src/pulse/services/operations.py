"""The orchestrator's actions, each changing the newsletter's state and saving it, and the
draft checks, which presenting a version also runs."""

import uuid
from collections.abc import Callable, Collection
from zoneinfo import ZoneInfo

from pulse.entities.base import Entity
from pulse.entities.clock import Clock
from pulse.entities.content import (
    CheckFailure,
    Content,
    Version,
    WriterOutput,
    check_content,
    item_sources,
    require_draft,
    version_of,
)
from pulse.entities.lifecycle import (
    Newsletter,
    next_version,
    open_newsletter,
    present,
    require_open,
    update,
)
from pulse.entities.mail import (
    Body,
    InboundEmail,
    Mailbox,
    MessageId,
    OutboundEmail,
    ScreenedEmail,
    subject,
)
from pulse.entities.review import Review, build_review
from pulse.entities.store import Store


class StartResult(Entity):
    newsletter_id: str
    opened: bool
    added: int
    rejected: int


class PresentResult(Entity):
    version: int


class Operations:
    def __init__(
        self,
        store: Store,
        submissions_mailbox: Mailbox,
        conversation_mailbox: Mailbox,
        screen: Callable[[InboundEmail], ScreenedEmail],
        render_review: Callable[[Version, Review], str],
        clock: Clock,
        *,
        reviewers: str,
        subject_template: str,
        timezone: ZoneInfo,
        categories: Collection[str],
        max_words: int,
    ) -> None:
        self._store = store
        self._submissions = submissions_mailbox
        self._conversation = conversation_mailbox
        self._screen = screen
        self._render_review = render_review
        self._clock = clock
        self._reviewers = reviewers
        self._subject_template = subject_template
        self._timezone = timezone
        self._categories = categories
        self._max_words = max_words

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
        inbox = await self._submissions.list_inbox()
        added = [self._screen(email) for email in inbox if email.message_id not in known]
        await self._store.save_start(newsletter, added)
        return StartResult(
            newsletter_id=newsletter.newsletter_id,
            opened=opened,
            added=len(added),
            rejected=sum(1 for s in added if s.rejection is not None),
        )

    async def check(self) -> list[CheckFailure]:
        """Run the draft checks on the working draft."""
        newsletter_id = require_open(await self._store.get_open_newsletter()).newsletter_id
        draft = await self._draft(newsletter_id)
        return await self._check(newsletter_id, draft.content)

    async def present_draft(self) -> PresentResult:
        """Save the working draft as the next version, with its check failures and the judge's
        latest verdicts, and email it to the reviewers, in the newsletter's email thread.

        The email is sent before the version is saved, so a failed send records nothing and
        a retried call presents the same version number again.
        """
        newsletter = require_open(await self._store.get_open_newsletter())
        newsletter_id = newsletter.newsletter_id
        draft = await self._draft(newsletter_id)
        version = version_of(
            draft,
            next_version(newsletter),
            self._clock.now(),
            await self._check(newsletter_id, draft.content),
            await self._store.get_verdicts(newsletter_id),
        )
        presented = present(newsletter)
        review = build_review(
            version,
            await self._store.get_items(newsletter_id),
            await self._store.list_extract_records(newsletter_id),
            await self._store.list_screened_emails(newsletter_id),
        )
        body = Body(content=self._render_review(version, review), content_type="html")
        sent_id = await self._email_reviewers(newsletter, body)
        await self._store.save_version(
            presented.model_copy(update={"thread_message_id": sent_id}), version
        )
        return PresentResult(version=version.version)

    async def _draft(self, newsletter_id: str) -> WriterOutput:
        return require_draft(await self._store.get_draft(newsletter_id))

    async def _check(self, newsletter_id: str, content: Content) -> list[CheckFailure]:
        consolidation = await self._store.get_items(newsletter_id)
        emails = await self._store.list_screened_emails(newsletter_id)
        feedback = await self._store.list_feedback(newsletter_id)
        return check_content(
            content,
            item_sources(content, consolidation.items if consolidation else [], emails),
            [message.text for message in feedback],
            self._categories,
            self._max_words,
        )

    async def _email_reviewers(self, newsletter: Newsletter, body: Body) -> MessageId:
        """Start the newsletter's email thread, or reply to its latest message."""
        if newsletter.thread_message_id is not None:
            return await self._conversation.reply(
                newsletter.thread_message_id, [self._reviewers], body
            )
        email = OutboundEmail(
            to=[self._reviewers],
            subject=subject(
                self._subject_template, newsletter.opened_at, self._timezone, prefix="Draft:"
            ),
            body=body,
            reply_to=None,
        )
        return await self._conversation.send(email)
