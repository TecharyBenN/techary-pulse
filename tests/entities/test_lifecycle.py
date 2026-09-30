from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from pulse.entities.errors import Refusal
from pulse.entities.lifecycle import (
    Newsletter,
    OnApproval,
    Scheduled,
    abandon,
    approve,
    is_due,
    mark_sent,
    open_newsletter,
    present,
    send_time,
    start_send,
    update,
    withdraw,
)

# The reviewer the channel verified, identified by Entra object ID.
REVIEWER = "3f1c0a52-7d4e-4b8a-9c61-2e5f8d9a0b17"
OPENED = datetime(2026, 9, 25, 16, 30, tzinfo=UTC)
NOW = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
SEND_TIME = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
LONDON = ZoneInfo("Europe/London")
MONDAY_NINE = Scheduled(mode="scheduled", day="MON", time=time(9, 0))


def _in_review(versions: int = 1) -> Newsletter:
    newsletter = open_newsletter("n-1", OPENED)
    for _ in range(versions):
        newsletter = present(newsletter)
    return newsletter


def _approved() -> Newsletter:
    return approve(_in_review(), 1, REVIEWER, "approve v1", NOW, MONDAY_NINE, LONDON)


def _started() -> Newsletter:
    return start_send(_approved())


def _sent() -> Newsletter:
    return mark_sent(_started(), SEND_TIME)


def _abandoned() -> Newsletter:
    return abandon(_in_review(), REVIEWER, NOW)


def test_open_newsletter_is_in_review_with_no_versions() -> None:
    newsletter = open_newsletter("n-1", OPENED)

    assert newsletter.newsletter_id == "n-1"
    assert newsletter.state == "in_review"
    assert newsletter.opened_at == OPENED
    assert newsletter.updated_at == OPENED
    assert newsletter.latest_version is None
    assert newsletter.approved_version is None
    assert not newsletter.send_started


def test_update_sets_updated_time() -> None:
    updated = update(_in_review(), NOW)

    assert updated.updated_at == NOW
    assert updated.opened_at == OPENED


def test_update_is_allowed_while_approved() -> None:
    assert update(_approved(), NOW).state == "approved"


def test_present_numbers_versions_from_one() -> None:
    newsletter = open_newsletter("n-1", OPENED)

    assert present(newsletter).latest_version == 1
    assert present(present(newsletter)).latest_version == 2


def test_approve_records_approval_and_send_time() -> None:
    approved = _approved()

    assert approved.state == "approved"
    assert approved.approved_version == 1
    assert approved.approver == REVIEWER
    assert approved.approved_at == NOW
    assert approved.send_time == SEND_TIME


def test_approve_on_approval_sends_at_approval_time() -> None:
    rule = OnApproval(mode="on_approval")

    approved = approve(_in_review(), 1, REVIEWER, "approve v1", NOW, rule, LONDON)

    assert approved.send_time == NOW


def test_approve_after_slot_sends_at_approval_time() -> None:
    late = SEND_TIME + timedelta(hours=2)

    approved = approve(_in_review(), 1, REVIEWER, "approve v1", late, MONDAY_NINE, LONDON)

    assert approved.send_time == late


def test_present_withdraws_approval_first() -> None:
    presented = present(_approved())

    assert presented.state == "in_review"
    assert presented.latest_version == 2
    assert presented.approved_version is None
    assert presented.approver is None
    assert presented.approved_at is None
    assert presented.send_time is None


def test_withdraw_returns_to_in_review_and_clears_approval() -> None:
    withdrawn = withdraw(_approved(), REVIEWER)

    assert withdrawn.state == "in_review"
    assert withdrawn.approved_version is None
    assert withdrawn.approver is None
    assert withdrawn.approved_at is None
    assert withdrawn.send_time is None


@pytest.mark.parametrize("newsletter", [_in_review(), _approved()], ids=["in_review", "approved"])
def test_abandon_closes_newsletter(newsletter: Newsletter) -> None:
    abandoned = abandon(newsletter, REVIEWER, NOW)

    assert abandoned.state == "abandoned"
    assert abandoned.closed_at == NOW


def test_start_send_records_send_started() -> None:
    started = _started()

    assert started.send_started
    assert started.state == "approved"


def test_mark_sent_closes_newsletter() -> None:
    sent = _sent()

    assert sent.state == "sent"
    assert sent.sent_at == SEND_TIME
    assert sent.closed_at == SEND_TIME


CLOSED = [_sent(), _abandoned()]
CLOSED_IDS = ["sent", "abandoned"]
UNCHANGEABLE = [*CLOSED, _started()]
UNCHANGEABLE_IDS = [*CLOSED_IDS, "send_started"]


@pytest.mark.parametrize("newsletter", UNCHANGEABLE, ids=UNCHANGEABLE_IDS)
def test_update_refuses(newsletter: Newsletter) -> None:
    with pytest.raises(Refusal):
        update(newsletter, NOW)


@pytest.mark.parametrize("newsletter", UNCHANGEABLE, ids=UNCHANGEABLE_IDS)
def test_present_refuses(newsletter: Newsletter) -> None:
    with pytest.raises(Refusal):
        present(newsletter)


@pytest.mark.parametrize(
    "newsletter", [*UNCHANGEABLE, _in_review()], ids=[*UNCHANGEABLE_IDS, "in_review"]
)
def test_withdraw_refuses(newsletter: Newsletter) -> None:
    with pytest.raises(Refusal):
        withdraw(newsletter, REVIEWER)


@pytest.mark.parametrize("newsletter", UNCHANGEABLE, ids=UNCHANGEABLE_IDS)
def test_abandon_refuses(newsletter: Newsletter) -> None:
    with pytest.raises(Refusal):
        abandon(newsletter, REVIEWER, NOW)


def test_withdraw_needs_a_reviewer() -> None:
    with pytest.raises(Refusal):
        withdraw(_approved(), None)


def test_abandon_needs_a_reviewer() -> None:
    with pytest.raises(Refusal):
        abandon(_in_review(), None, NOW)


@pytest.mark.parametrize(
    "message",
    [
        "approve v2",
        "APPROVE V2",
        "  approve v2  ",
        "\n\n   \napprove v2\nLooks great, thanks.",
        "Approve v2\r\n",
    ],
)
def test_approval_message_accepted(message: str) -> None:
    approved = approve(_in_review(2), 2, REVIEWER, message, NOW, MONDAY_NINE, LONDON)

    assert approved.approved_version == 2


@pytest.mark.parametrize(
    "message",
    [
        "",
        "   \n  ",
        "approve",
        "approve v1",
        "approve v2 please",
        "approve  v2",
        "approvev2",
        "Looks great.\napprove v2",
        "Please approve v2",
    ],
)
def test_approval_message_refused(message: str) -> None:
    with pytest.raises(Refusal):
        approve(_in_review(2), 2, REVIEWER, message, NOW, MONDAY_NINE, LONDON)


def test_approve_needs_a_reviewer() -> None:
    with pytest.raises(Refusal):
        approve(_in_review(), 1, None, "approve v1", NOW, MONDAY_NINE, LONDON)


def test_approve_needs_a_reviewer_message() -> None:
    with pytest.raises(Refusal):
        approve(_in_review(), 1, REVIEWER, None, NOW, MONDAY_NINE, LONDON)


@pytest.mark.parametrize("version", [1, 3])
def test_approve_needs_latest_version(version: int) -> None:
    message = f"approve v{version}"

    with pytest.raises(Refusal):
        approve(_in_review(2), version, REVIEWER, message, NOW, MONDAY_NINE, LONDON)


def test_approve_needs_a_presented_version() -> None:
    with pytest.raises(Refusal):
        approve(
            open_newsletter("n-1", OPENED),
            1,
            REVIEWER,
            "approve v1",
            NOW,
            MONDAY_NINE,
            LONDON,
        )


@pytest.mark.parametrize(
    "newsletter", [_approved(), *UNCHANGEABLE], ids=["approved", *UNCHANGEABLE_IDS]
)
def test_approve_needs_in_review(newsletter: Newsletter) -> None:
    with pytest.raises(Refusal):
        approve(newsletter, 1, REVIEWER, "approve v1", NOW, MONDAY_NINE, LONDON)


def test_refusal_gives_a_reason() -> None:
    with pytest.raises(Refusal, match="v1"):
        approve(_in_review(2), 1, REVIEWER, "approve v1", NOW, MONDAY_NINE, LONDON)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (SEND_TIME - timedelta(seconds=1), False),
        (SEND_TIME, True),
        (SEND_TIME + timedelta(days=1), True),
    ],
)
def test_is_due_at_send_time(now: datetime, expected: bool) -> None:
    assert is_due(_approved(), now) is expected


@pytest.mark.parametrize(
    "newsletter",
    [_in_review(), *UNCHANGEABLE],
    ids=["in_review", *UNCHANGEABLE_IDS],
)
def test_is_due_only_for_approved_unsent(newsletter: Newsletter) -> None:
    assert not is_due(newsletter, SEND_TIME + timedelta(days=30))


@pytest.mark.parametrize(
    "newsletter", [_in_review(), *UNCHANGEABLE], ids=["in_review", *UNCHANGEABLE_IDS]
)
def test_start_send_needs_approved_unsent(newsletter: Newsletter) -> None:
    with pytest.raises(Refusal):
        start_send(newsletter)


def test_start_send_rechecks_latest_version() -> None:
    stale = _approved().model_copy(update={"latest_version": 2})

    with pytest.raises(Refusal):
        start_send(stale)


@pytest.mark.parametrize("newsletter", [_approved(), *CLOSED], ids=["approved", *CLOSED_IDS])
def test_mark_sent_needs_send_started(newsletter: Newsletter) -> None:
    with pytest.raises(Refusal):
        mark_sent(newsletter, SEND_TIME)


def _london(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LONDON)


def test_on_approval_sends_at_approval_time() -> None:
    assert send_time(OnApproval(mode="on_approval"), LONDON, OPENED, NOW) == NOW


@pytest.mark.parametrize(
    ("opened", "approved", "expected"),
    [
        # Opened Friday, approved Saturday: the following Monday.
        (_london(2026, 9, 25, 17, 30), _london(2026, 9, 26, 10, 0), _london(2026, 9, 28, 9, 0)),
        # Opened Monday before nine: that Monday.
        (_london(2026, 9, 28, 8, 0), _london(2026, 9, 28, 8, 30), _london(2026, 9, 28, 9, 0)),
        # Opened exactly at the slot: the next week's slot.
        (_london(2026, 9, 28, 9, 0), _london(2026, 9, 28, 10, 0), _london(2026, 10, 5, 9, 0)),
        # Opened Monday after nine: the next Monday.
        (_london(2026, 9, 28, 9, 30), _london(2026, 9, 29, 10, 0), _london(2026, 10, 5, 9, 0)),
    ],
)
def test_scheduled_sends_at_first_slot_after_opening(
    opened: datetime, approved: datetime, expected: datetime
) -> None:
    assert send_time(MONDAY_NINE, LONDON, opened, approved) == expected


def test_scheduled_slot_passed_sends_at_approval_time() -> None:
    opened = _london(2026, 9, 25, 17, 30)
    approved = _london(2026, 9, 28, 11, 0)

    assert send_time(MONDAY_NINE, LONDON, opened, approved) == approved


def test_scheduled_slot_uses_local_time_across_daylight_saving_change() -> None:
    # British Summer Time ends on Sunday 25 October 2026.
    opened = datetime(2026, 10, 23, 16, 30, tzinfo=UTC)
    approved = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)

    result = send_time(MONDAY_NINE, LONDON, opened, approved)

    assert result == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    assert result.tzinfo == UTC


def test_scheduled_slot_in_summer_time_is_returned_in_utc() -> None:
    result = send_time(MONDAY_NINE, LONDON, _london(2026, 9, 25, 17, 30), NOW)

    assert result == datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
    assert result.tzinfo == UTC
