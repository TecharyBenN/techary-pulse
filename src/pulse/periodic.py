"""The periodic check: sends a due approved edition, or expires an overdue one.

This is the only module that reads ``config.all_staff``, so no other code path can send to it.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pulse import editions
from pulse.config import Config, is_reviewer
from pulse.mail import Mailbox
from pulse.models import Edition, OutgoingEmail
from pulse.pipeline.render import edition_subject, notice_email, render_newsletter
from pulse.pipeline.runner import Clock
from pulse.store import EditionStore

log = logging.getLogger("pulse.periodic")

CheckStatus = Literal["sent", "expired", "send_alert", "refused", "nothing_due"]


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    edition_id: str | None = None


async def _send(
    config: Config, conversation: Mailbox, store: EditionStore, edition: Edition, now: datetime
) -> CheckStatus:
    version = await store.current_version(edition.id)
    # Re-check the approval immediately before sending, since only this and the approve tool
    # ever act on it, and nothing else may have invalidated it since due_to_send was computed.
    approver_ok = edition.approver is not None and is_reviewer(config, edition.approver)
    if edition.approved_version != version.number or not approver_ok:
        log.error("approval re-check failed before send", extra={"conversation_id": edition.id})
        return "refused"

    started = editions.mark_send_started(edition)
    await store.update_edition(started)

    await conversation.send(
        OutgoingEmail(
            to=[config.all_staff],
            reply_to=[config.mailboxes.submissions],
            subject=edition_subject(config, edition.created_at),
            html=render_newsletter(config, version.headline, version.draft),
        )
    )

    sent = editions.mark_sent(started, now)
    await store.update_edition(sent)

    await conversation.send(
        notice_email(
            config, edition.created_at, "Sent", f"Version {version.number} was sent to all staff."
        )
    )
    log.info("edition sent", extra={"conversation_id": edition.id, "version": version.number})
    return "sent"


async def _expire(
    config: Config, conversation: Mailbox, store: EditionStore, edition: Edition, now: datetime
) -> CheckStatus:
    expired = editions.expire(edition, now)
    await store.update_edition(expired)
    await conversation.send(
        notice_email(
            config,
            edition.created_at,
            "Closed unsent",
            "The edition expired unapproved and was closed.",
        )
    )
    log.info("edition expired", extra={"conversation_id": edition.id})
    return "expired"


async def run_periodic_check(
    config: Config, conversation: Mailbox, store: EditionStore, clock: Clock
) -> CheckResult:
    """Run the periodic check once: send a due approved edition, or expire an overdue one.

    Raises:
        GraphError: If the send itself fails, after `send_started` is recorded; the edition
            stays `approved` with `send_started` set, for the "send_started without sent" alert
            to find, and is never resent automatically.
    """
    edition = await store.open_edition()
    if edition is None:
        return CheckResult("nothing_due")

    if edition.state == "approved" and edition.send_started:
        log.warning("send_started without sent", extra={"conversation_id": edition.id})
        return CheckResult("send_alert", edition.id)

    now = clock()

    if editions.due_to_send(edition, now):
        status = await _send(config, conversation, store, edition, now)
        return CheckResult(status, edition.id)

    if editions.due_to_expire(edition, now, config.edition.expire_after_days):
        status = await _expire(config, conversation, store, edition, now)
        return CheckResult(status, edition.id)

    return CheckResult("nothing_due", edition.id)
