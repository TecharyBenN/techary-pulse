"""Edition transitions: pure functions returning a new `Edition`, or refusing.

Each transition validates its own preconditions from the design's edition state diagram and
raises `EditionRefused` with the reason when they are not met. None of these functions perform
any side effect; callers save the returned edition to the store.
"""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pulse.config import Config, SendConfig, is_reviewer
from pulse.errors import EditionRefused
from pulse.models import Edition

_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def send_time(approved_at: datetime, send: SendConfig, timezone: str) -> datetime:
    """Return the UTC time an edition approved at `approved_at` should be sent.

    In `on_approval` mode, this is `approved_at`. In `scheduled` mode, it is the next
    occurrence of `send.day` at `send.time`, in `timezone`, at or after `approved_at`.
    """
    if send.mode == "on_approval":
        return approved_at
    assert send.day is not None and send.time is not None  # SendConfig enforces this
    zone = ZoneInfo(timezone)
    local = approved_at.astimezone(zone)
    days_ahead = (_WEEKDAYS[send.day] - local.weekday()) % 7
    candidate = local.date() + timedelta(days=days_ahead)
    result = datetime.combine(candidate, send.time, tzinfo=zone)
    if result < local:
        result += timedelta(days=7)
    return result.astimezone(UTC)


def approve(
    edition: Edition,
    *,
    version: int,
    caller: str,
    message: str,
    config: Config,
    now: datetime,
) -> Edition:
    """Record approval of `version`, which must be the edition's current version.

    Raises:
        EditionRefused: If the caller is not a reviewer, the edition is not `in_review`,
            `version` is not current, or the reviewer's own message lacks `approve v{version}`
            as a whole token, so `approve v1` never matches `approve v12`.
    """
    if not is_reviewer(config, caller):
        raise EditionRefused(f"{caller} is not a reviewer")
    if edition.state != "in_review":
        raise EditionRefused(f"edition is {edition.state}, not in_review")
    if version != edition.current_version:
        raise EditionRefused(
            f"version {version} is not the current version {edition.current_version}"
        )
    if not re.search(rf"\bapprove\s+v{version}\b", message, re.IGNORECASE):
        raise EditionRefused(f"the reviewer's message does not contain 'approve v{version}'")
    return edition.model_copy(
        update={
            "state": "approved",
            "approved_version": version,
            "approver": caller,
            "approved_at": now,
            "send_at": send_time(now, config.send, config.timezone),
        }
    )


def withdraw(edition: Edition) -> Edition:
    """Return an approved edition to `in_review`, clearing its approval and send time.

    Raises:
        EditionRefused: If the edition is not `approved`, or the send has started.
    """
    if edition.state != "approved":
        raise EditionRefused(f"edition is {edition.state}, not approved")
    if edition.send_started:
        raise EditionRefused("the send has already started")
    return edition.model_copy(
        update={
            "state": "in_review",
            "approved_version": None,
            "approver": None,
            "approved_at": None,
            "send_at": None,
        }
    )


def new_version(edition: Edition) -> Edition:
    """Move the edition to its next version, withdrawing an approval first if there is one.

    Raises:
        EditionRefused: If the send has already started.
    """
    if edition.send_started:
        raise EditionRefused("the send has already started")
    if edition.state == "approved":
        edition = withdraw(edition)
    return edition.model_copy(update={"current_version": edition.current_version + 1})


def discard(edition: Edition, now: datetime) -> Edition:
    """Close an `in_review` edition unsent.

    Raises:
        EditionRefused: If the edition is not `in_review`.
    """
    if edition.state != "in_review":
        raise EditionRefused(f"edition is {edition.state}, not in_review")
    return edition.model_copy(update={"state": "discarded", "closed_at": now})


def mark_send_started(edition: Edition) -> Edition:
    """Record that the periodic check has begun sending the edition."""
    return edition.model_copy(update={"send_started": True})


def mark_sent(edition: Edition, now: datetime) -> Edition:
    """Mark the edition sent and closed."""
    return edition.model_copy(update={"state": "sent", "sent_at": now, "closed_at": now})


def expire(edition: Edition, now: datetime) -> Edition:
    """Close an `in_review` edition as expired, unsent.

    Raises:
        EditionRefused: If the edition is not `in_review`.
    """
    if edition.state != "in_review":
        raise EditionRefused(f"edition is {edition.state}, not in_review")
    return edition.model_copy(update={"state": "expired", "closed_at": now})


def due_to_send(edition: Edition, now: datetime) -> bool:
    """Whether an approved edition's send time has come."""
    return (
        edition.state == "approved"
        and not edition.send_started
        and edition.send_at is not None
        and edition.send_at <= now
    )


def due_to_expire(edition: Edition, now: datetime, expire_after_days: int | None) -> bool:
    """Whether an `in_review` edition is older than `expire_after_days`."""
    if expire_after_days is None or edition.state != "in_review":
        return False
    return now - edition.created_at >= timedelta(days=expire_after_days)
