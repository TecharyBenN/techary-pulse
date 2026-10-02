"""Operations: the newsletter actions that change the store, and the draft checks."""

import uuid
from collections.abc import Callable, Collection
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime

from pulse.entities import lifecycle
from pulse.entities.base import Entity
from pulse.entities.content import (
    CheckFailure,
    Content,
    Version,
    WriterOutput,
    check_content,
    draft_changed,
    item_sources,
    require_draft,
    version_of,
)
from pulse.entities.errors import Refusal
from pulse.entities.extracts import restored
from pulse.entities.lifecycle import (
    Newsletter,
    OnApproval,
    Scheduled,
    cancel_approval,
    next_version,
    open_newsletter,
    present,
    require_changeable,
    require_latest,
    require_open,
    send_time,
    update,
)
from pulse.entities.mail import Body, InboundEmail, Mailbox, MessageId, ScreenedEmail
from pulse.entities.review import Review, build_review
from pulse.entities.store import Store
from pulse.services.outbox import Outbox

_SEND_CANCELLED = "Send cancelled"
_ABANDONED = "Abandoned"


class StartResult(Entity):
    """What start_newsletter did: the newsletter, whether it opened, and the emails added and
    rejected."""

    newsletter_id: str
    opened: bool
    added: int
    rejected: int


class PresentResult(Entity):
    """The version present_draft emailed, and whether it resent it or withdrew an approval."""

    version: int
    # Whether the draft was unchanged, so the latest version was emailed again as it was.
    resent: bool = False
    # Whether presenting withdrew an approval, and whether the reviewers were told so.
    approval_withdrawn: bool = False
    notice_sent: bool | None = None


class RestoreResult(Entity):
    """The excluded record restore included."""

    excluded_id: str
    message_id: MessageId


class ApproveResult(Entity):
    """The approved version and its send time."""

    version: int
    # In the configured time zone, for the reply.
    send_time: AwareDatetime


class NoticeResult(Entity):
    """Whether the reviewers were emailed about a change that is already saved."""

    notice_sent: bool


class Operations:
    """Starts, restores, presents, approves, withdraws and abandons a newsletter, and runs the
    draft checks."""

    def __init__(
        self,
        store: Store,
        submissions_mailbox: Mailbox,
        outbox: Outbox,
        screen: Callable[[InboundEmail], ScreenedEmail],
        render_review: Callable[[Version, Review, str | None], str],
        clock: Callable[[], datetime],
        *,
        send_rule: OnApproval | Scheduled,
        timezone: ZoneInfo,
        categories: Collection[str],
        max_words: int,
    ) -> None:
        self._store = store
        self._submissions = submissions_mailbox
        self._outbox = outbox
        self._screen = screen
        self._render_review = render_review
        self._clock = clock
        self._send_rule = send_rule
        self._timezone = timezone
        self._categories = categories
        self._max_words = max_words

    async def start_newsletter(self) -> StartResult:
        """Open a newsletter with every pending email, or add those new to the open one."""
        now = self._clock()
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

    async def restore(self, excluded_id: str, caller: str | None) -> RestoreResult:
        """Include the named excluded record, recording the reviewer who asked."""
        newsletter_id = require_open(await self._store.get_open_newsletter()).newsletter_id
        records = await self._store.list_extract_records(newsletter_id)
        record = restored(records, excluded_id, caller)
        await self._store.save_restored(newsletter_id, record)
        return RestoreResult(excluded_id=excluded_id, message_id=record.message_id)

    async def check(self) -> list[CheckFailure]:
        """Run the draft checks on the latest newsletter's working draft."""
        newsletter = require_latest(await self._store.get_latest_newsletter())
        draft = await self.draft_or_version(newsletter, None)
        return await self._check(newsletter.newsletter_id, draft.content)

    async def present_draft(self, email_reviewers: bool = True) -> PresentResult:
        """Save the working draft as the next version, with its check failures and the judge's
        latest verdicts, and email it to the reviewers, in the newsletter's email thread. When
        the draft is unchanged since the latest version, email that version again instead.

        An approval is withdrawn, and the reviewers told, before anything else, so the approved
        version can no longer be sent. The version's email is sent before the version is saved,
        so a failed send records nothing and a retried call presents the same version number
        again. With `email_reviewers` False, the email channel's reply carries the version
        instead, so reviewers receive one email.
        """
        newsletter = require_open(await self._store.get_open_newsletter())
        newsletter_id = newsletter.newsletter_id
        draft = await self.draft_or_version(newsletter, None)
        latest = await self.latest_version(newsletter)
        if latest is not None and not draft_changed(draft, latest):
            return await self._resend(newsletter, latest, email_reviewers)
        withdrawn = None
        if newsletter.state == "approved":
            newsletter, withdrawn = await self._withdrawn(newsletter, cancel_approval(newsletter))
        version = version_of(
            draft,
            next_version(newsletter),
            self._clock(),
            await self._check(newsletter_id, draft.content),
            await self._store.get_verdicts(newsletter_id),
        )
        presented = present(newsletter)
        if email_reviewers:
            body = await self.reviewer_email(newsletter, version)
            sent_id = await self._outbox.to_reviewers(newsletter, body)
            presented = presented.model_copy(update={"thread_message_id": sent_id})
        await self._store.save_version(presented, version)
        if withdrawn is None:
            return PresentResult(version=version.version)
        return PresentResult(
            version=version.version, approval_withdrawn=True, notice_sent=withdrawn
        )

    async def approve(self, version: int, caller: str | None, message: str | None) -> ApproveResult:
        """Record the reviewer's approval of the named version and set its send time."""
        newsletter = require_open(await self._store.get_open_newsletter())
        now = self._clock()
        send_at = send_time(self._send_rule, self._timezone, newsletter.opened_at, now)
        approved = lifecycle.approve(newsletter, version, caller, message, now, send_at)
        await self._store.save_newsletter(approved)
        return ApproveResult(version=version, send_time=send_at.astimezone(self._timezone))

    async def withdraw_approval(self, caller: str | None) -> NoticeResult:
        """Return the approved newsletter to review, and tell the reviewers the send is
        cancelled."""
        newsletter = require_open(await self._store.get_open_newsletter())
        _, sent = await self._withdrawn(newsletter, lifecycle.withdraw(newsletter, caller))
        return NoticeResult(notice_sent=sent)

    async def abandon(self, caller: str | None) -> NoticeResult:
        """Close the newsletter unsent, and tell the reviewers."""
        newsletter = require_open(await self._store.get_open_newsletter())
        abandoned = lifecycle.abandon(newsletter, caller, self._clock())
        await self._store.save_newsletter(abandoned)
        text = "This newsletter was abandoned and will not be sent. Its emails stay pending."
        _, sent = await self._outbox.notice(abandoned, _ABANDONED, text)
        return NoticeResult(notice_sent=sent)

    async def _resend(
        self, newsletter: Newsletter, latest: Version, email_reviewers: bool
    ) -> PresentResult:
        """Email the latest version again as it was presented, changing nothing else."""
        require_changeable(newsletter)
        if email_reviewers:
            await self._outbox.thread(newsletter, await self.reviewer_email(newsletter, latest))
        return PresentResult(version=latest.version, resent=True)

    async def reviewer_email(
        self, newsletter: Newsletter, version: Version, reply: str | None = None
    ) -> Body:
        """The version's reviewer email, after the orchestrator's reply when there is one."""
        review = await self._review(newsletter.newsletter_id, version)
        return Body(content=self._render_review(version, review, reply), content_type="html")

    async def latest_version(self, newsletter: Newsletter) -> Version | None:
        latest = newsletter.latest_version
        return await self._store.get_version(newsletter.newsletter_id, latest) if latest else None

    async def draft_or_version(self, newsletter: Newsletter, version: int | None) -> WriterOutput:
        """The working draft, or the named version; refuses when there is none."""
        newsletter_id = newsletter.newsletter_id
        if version is None:
            return require_draft(await self._store.get_draft(newsletter_id))
        presented = await self._store.get_version(newsletter_id, version)
        if presented is None:
            raise Refusal(f"v{version} has not been presented")
        return presented

    async def _review(self, newsletter_id: str, version: Version) -> Review:
        return build_review(
            version,
            await self._store.get_items(newsletter_id),
            await self._store.list_extract_records(newsletter_id),
            await self._store.list_screened_emails(newsletter_id),
        )

    async def _withdrawn(
        self, approved: Newsletter, withdrawn: Newsletter
    ) -> tuple[Newsletter, bool]:
        """Save the withdrawal before the notice, so a failed notice never leaves the approved
        version to be sent."""
        await self._store.save_newsletter(withdrawn)
        version = approved.approved_version
        text = f"The approval of version {version} was withdrawn, so it will not be sent."
        return await self._outbox.notice(withdrawn, _SEND_CANCELLED, text)

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
