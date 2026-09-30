"""The newsletter lifecycle: states, transitions, send time and the approval check."""

import datetime as dt
import re
from typing import Annotated, Literal, get_args
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, PositiveInt, field_validator

from pulse.entities.base import Entity, StrictEntity
from pulse.entities.errors import Refusal

State = Literal["in_review", "approved", "sent", "abandoned"]
Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

_WEEKDAYS: tuple[Weekday, ...] = get_args(Weekday)
_HH_MM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
# A newsletter in any other state is open.
CLOSED_STATES: tuple[State, ...] = ("sent", "abandoned")
_NO_APPROVAL = {"approved_version": None, "approver": None, "approved_at": None, "send_time": None}


class OnApproval(StrictEntity):
    mode: Literal["on_approval"]


class Scheduled(StrictEntity):
    mode: Literal["scheduled"]
    day: Weekday
    time: dt.time

    @field_validator("time", mode="before")
    @classmethod
    def _hh_mm(cls, value: object) -> object:
        # YAML gives a string; code may build the rule with a time directly.
        if not (isinstance(value, dt.time) or (isinstance(value, str) and _HH_MM.fullmatch(value))):
            raise ValueError("send.time must be a 24-hour HH:MM time")
        return value


SendRule = Annotated[OnApproval | Scheduled, Field(discriminator="mode")]


class Newsletter(Entity):
    newsletter_id: str
    state: State
    opened_at: AwareDatetime
    updated_at: AwareDatetime
    latest_version: PositiveInt | None
    approved_version: PositiveInt | None
    approver: str | None
    approved_at: AwareDatetime | None
    send_time: AwareDatetime | None
    send_started: bool
    sent_at: AwareDatetime | None
    closed_at: AwareDatetime | None


def open_newsletter(newsletter_id: str, now: dt.datetime) -> Newsletter:
    return Newsletter(
        newsletter_id=newsletter_id,
        state="in_review",
        opened_at=now,
        updated_at=now,
        latest_version=None,
        approved_version=None,
        approver=None,
        approved_at=None,
        send_time=None,
        send_started=False,
        sent_at=None,
        closed_at=None,
    )


def update(newsletter: Newsletter, now: dt.datetime) -> Newsletter:
    """Record that `start_newsletter` added the emails that arrived since."""
    _require_changeable(newsletter)
    return newsletter.model_copy(update={"updated_at": now})


def present(newsletter: Newsletter) -> Newsletter:
    """Number the next version, withdrawing any approval first."""
    _require_changeable(newsletter)
    return newsletter.model_copy(
        update={"state": "in_review", "latest_version": (newsletter.latest_version or 0) + 1}
        | _NO_APPROVAL
    )


def approve(
    newsletter: Newsletter,
    version: int,
    caller: str | None,
    message: str | None,
    now: dt.datetime,
    rule: OnApproval | Scheduled,
    timezone: ZoneInfo,
) -> Newsletter:
    """The approval check; `caller` and `message` come from the run, never from the model.

    `caller` is the reviewer the channel verified, or None in a run with no reviewer.
    """
    caller = _require_reviewer(caller)
    if newsletter.state != "in_review":
        raise Refusal(f"the newsletter is {newsletter.state}, not in review")
    if version != newsletter.latest_version:
        raise Refusal(f"v{version} is not the latest presented version")
    if _first_line(message).casefold() != f"approve v{version}":
        raise Refusal(f"the reviewer's message does not start with approve v{version}")
    return newsletter.model_copy(
        update={
            "state": "approved",
            "approved_version": version,
            "approver": caller,
            "approved_at": now,
            "send_time": send_time(rule, timezone, newsletter.opened_at, now),
        }
    )


def withdraw(newsletter: Newsletter, caller: str | None) -> Newsletter:
    _require_reviewer(caller)
    _require_changeable(newsletter)
    if newsletter.state != "approved":
        raise Refusal("the newsletter is not approved")
    return newsletter.model_copy(update={"state": "in_review"} | _NO_APPROVAL)


def abandon(newsletter: Newsletter, caller: str | None, now: dt.datetime) -> Newsletter:
    _require_reviewer(caller)
    _require_changeable(newsletter)
    return newsletter.model_copy(update={"state": "abandoned", "closed_at": now})


def is_due(newsletter: Newsletter, now: dt.datetime) -> bool:
    return (
        newsletter.state == "approved"
        and not newsletter.send_started
        and newsletter.send_time is not None
        and newsletter.send_time <= now
    )


def start_send(newsletter: Newsletter) -> Newsletter:
    """Re-check the approval and record `send_started`."""
    if newsletter.state != "approved" or newsletter.send_started:
        raise Refusal("the newsletter is not approved and awaiting its send")
    if newsletter.approved_version != newsletter.latest_version:
        raise Refusal("the approved version is not the latest presented version")
    return newsletter.model_copy(update={"send_started": True})


def mark_sent(newsletter: Newsletter, now: dt.datetime) -> Newsletter:
    if newsletter.state != "approved" or not newsletter.send_started:
        raise Refusal("the send has not started")
    return newsletter.model_copy(update={"state": "sent", "sent_at": now, "closed_at": now})


def send_time(
    rule: OnApproval | Scheduled,
    timezone: ZoneInfo,
    opened_at: dt.datetime,
    approved_at: dt.datetime,
) -> dt.datetime:
    if isinstance(rule, OnApproval):
        return approved_at
    return max(_send_slot(rule, timezone, opened_at), approved_at)


def _send_slot(rule: Scheduled, timezone: ZoneInfo, opened_at: dt.datetime) -> dt.datetime:
    """The first occurrence of the send day and time, in `timezone`, after the opening."""
    opened = opened_at.astimezone(timezone)
    days_ahead = (_WEEKDAYS.index(rule.day) - opened.weekday()) % 7
    day = opened.date() + dt.timedelta(days=days_ahead)
    slot = dt.datetime.combine(day, rule.time, tzinfo=timezone)
    if slot <= opened:
        slot = dt.datetime.combine(day + dt.timedelta(days=7), rule.time, tzinfo=timezone)
    return slot.astimezone(dt.UTC)


def _require_reviewer(caller: str | None) -> str:
    if caller is None:
        raise Refusal("no reviewer is present in this run")
    return caller


def _require_changeable(newsletter: Newsletter) -> None:
    if newsletter.state in CLOSED_STATES:
        raise Refusal(f"the newsletter is {newsletter.state}")
    if newsletter.send_started:
        raise Refusal("the send has started")


def _first_line(message: str | None) -> str:
    lines = (line.strip() for line in (message or "").splitlines())
    return next((line for line in lines if line), "")
