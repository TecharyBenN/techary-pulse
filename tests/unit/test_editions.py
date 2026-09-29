from datetime import UTC, datetime, time

import pytest

from pulse import editions
from pulse.config import SendConfig, load_config
from pulse.errors import EditionRefused
from pulse.models import Edition

from ..conftest import CONFIG_DIR
from ..support import REVIEWER, at

ON_APPROVAL = SendConfig(mode="on_approval")
SCHEDULED = SendConfig(mode="scheduled", day="MON", time=time(9, 0))
CONFIG = load_config(CONFIG_DIR / "config.example.yaml").model_copy(
    update={"reviewers": [REVIEWER, "other@techary.ai"], "send": ON_APPROVAL}
)


def edition(**overrides: object) -> Edition:
    kwargs: dict[str, object] = dict(
        id="e1",
        build_id="b1",
        state="in_review",
        trigger="command",
        created_at=datetime(2026, 9, 21, 9, tzinfo=UTC),
        current_version=1,
    )
    kwargs.update(overrides)
    return Edition.model_validate(kwargs)


def approve(
    message: str = "approve v1", target: Edition | None = None, caller: str = REVIEWER
) -> Edition:
    return editions.approve(
        target or edition(),
        version=1,
        caller=caller,
        message=message,
        config=CONFIG,
        now=at(21, 10),
    )


# approve


def test_approve_sets_approver_time_and_send_at() -> None:
    result = approve("approve v1 please")
    assert result.state == "approved"
    assert result.approved_version == 1
    assert result.approver == REVIEWER
    assert result.approved_at == at(21, 10)
    assert result.send_at == at(21, 10)


def test_approve_matches_the_phrase_ignoring_case() -> None:
    assert approve("APPROVE V1").state == "approved"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"message": "approve v12"}, "does not contain"),
        ({"message": "looks good to me"}, "does not contain"),
        ({"caller": "outsider@techary.ai"}, "not a reviewer"),
        ({"target": edition(state="approved")}, "not in_review"),
        ({"target": edition(current_version=2)}, "not the current version"),
    ],
)
def test_approve_refusals(kwargs: dict[str, object], reason: str) -> None:
    with pytest.raises(EditionRefused, match=reason):
        approve(**kwargs)  # type: ignore[arg-type]


def test_approve_accepts_a_reviewer_address_in_any_case() -> None:
    assert approve(caller="Reviewer@Techary.AI").state == "approved"


# withdraw


def test_withdraw_returns_to_in_review_and_clears_approval() -> None:
    approved = edition(
        state="approved",
        approved_version=1,
        approver=REVIEWER,
        approved_at=at(21),
        send_at=at(22),
    )
    result = editions.withdraw(approved)
    assert result.state == "in_review"
    assert result.approved_version is None
    assert result.approver is None
    assert result.approved_at is None
    assert result.send_at is None


def test_withdraw_refuses_when_not_approved() -> None:
    with pytest.raises(EditionRefused, match="not approved"):
        editions.withdraw(edition())


def test_withdraw_refuses_once_send_started() -> None:
    with pytest.raises(EditionRefused, match="already started"):
        editions.withdraw(edition(state="approved", send_started=True))


# new_version


def test_new_version_increments_current_version() -> None:
    result = editions.new_version(edition(current_version=1))
    assert result.current_version == 2


def test_new_version_withdraws_an_approval_first() -> None:
    approved = edition(state="approved", current_version=1, approved_version=1, approver=REVIEWER)
    result = editions.new_version(approved)
    assert result.state == "in_review"
    assert result.current_version == 2
    assert result.approved_version is None


def test_new_version_refuses_once_send_started() -> None:
    with pytest.raises(EditionRefused, match="already started"):
        editions.new_version(edition(state="approved", send_started=True))


# discard


def test_discard_closes_an_in_review_edition() -> None:
    result = editions.discard(edition(), now=at(21))
    assert result.state == "discarded"
    assert result.closed_at == at(21)


def test_discard_refuses_when_not_in_review() -> None:
    with pytest.raises(EditionRefused, match="not in_review"):
        editions.discard(edition(state="approved"), now=at(21))


# mark_send_started, mark_sent, expire


def test_mark_send_started_sets_the_flag() -> None:
    assert editions.mark_send_started(edition(state="approved")).send_started is True


def test_mark_sent_sets_state_and_closed_at() -> None:
    result = editions.mark_sent(edition(state="approved", send_started=True), now=at(22))
    assert result.state == "sent"
    assert result.sent_at == at(22)
    assert result.closed_at == at(22)


def test_expire_sets_state_and_closed_at() -> None:
    result = editions.expire(edition(), now=at(28))
    assert result.state == "expired"
    assert result.closed_at == at(28)


def test_expire_refuses_when_not_in_review() -> None:
    with pytest.raises(EditionRefused, match="not in_review"):
        editions.expire(edition(state="approved"), now=at(28))


# send_time


def test_send_time_on_approval_is_the_approval_time() -> None:
    assert editions.send_time(at(21, 14, 5), ON_APPROVAL, "Europe/London") == at(21, 14, 5)


def test_send_time_scheduled_is_the_next_monday_at_the_configured_time() -> None:
    # 21 September 2026 is a Monday; approval lands after 09:00 local, so the next Monday.
    result = editions.send_time(at(21, 14), SCHEDULED, "Europe/London")
    assert result == datetime(2026, 9, 28, 8, 0, tzinfo=UTC)  # 09:00 BST = 08:00 UTC


def test_send_time_scheduled_same_day_before_the_slot_uses_today() -> None:
    result = editions.send_time(at(21, 6), SCHEDULED, "Europe/London")
    assert result == datetime(2026, 9, 21, 8, 0, tzinfo=UTC)  # 09:00 BST = 08:00 UTC


def test_send_time_scheduled_crosses_a_daylight_saving_change() -> None:
    # Approval on Friday 23 October 2026 (BST); the next Monday, 26 October, is after the UK
    # clocks go back on 25 October, so the send time is 09:00 GMT, not 09:00 BST.
    approved_at = datetime(2026, 10, 23, 12, tzinfo=UTC)
    result = editions.send_time(approved_at, SCHEDULED, "Europe/London")
    assert result == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 09:00 GMT = 09:00 UTC


# due_to_send, due_to_expire


def test_due_to_send_when_approved_and_send_at_has_passed() -> None:
    approved = edition(state="approved", send_at=at(21, 9))
    assert editions.due_to_send(approved, now=at(21, 9)) is True
    assert editions.due_to_send(approved, now=at(21, 8)) is False


def test_due_to_send_false_once_send_started() -> None:
    approved = edition(state="approved", send_at=at(21, 9), send_started=True)
    assert editions.due_to_send(approved, now=at(21, 9)) is False


def test_due_to_expire_false_with_no_expiry_configured() -> None:
    assert (
        editions.due_to_expire(edition(created_at=at(1)), now=at(28), expire_after_days=None)
        is False
    )


def test_due_to_expire_true_once_old_enough() -> None:
    old = edition(created_at=at(1))
    assert editions.due_to_expire(old, now=at(8), expire_after_days=7) is True
    assert editions.due_to_expire(old, now=at(7), expire_after_days=7) is False


def test_due_to_expire_false_when_not_in_review() -> None:
    approved = edition(state="approved", created_at=at(1))
    assert editions.due_to_expire(approved, now=at(28), expire_after_days=7) is False
