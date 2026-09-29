from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pulse.config import Config, EditionConfig, SendConfig, load_config
from pulse.models import Version
from pulse.periodic import run_periodic_check
from pulse.store import EditionStore

from ..support import DRAFT_V1, REVIEWER, FakeMailbox, approve_seeded, at, seed_edition

pytestmark = pytest.mark.anyio


def clock(now: datetime) -> Callable[[], datetime]:
    return lambda: now


@pytest.fixture
def config(config_dir: Path) -> Config:
    return load_config(config_dir / "config.example.yaml")


@pytest.fixture
def store(tmp_path: Path) -> EditionStore:
    return EditionStore(tmp_path / "state" / "pulse.db")


@pytest.fixture
def conversation() -> FakeMailbox:
    return FakeMailbox([])


# nothing due


async def test_nothing_due_with_no_open_edition(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    result = await run_periodic_check(config, conversation, store, clock(at(25)))
    assert result.status == "nothing_due"
    assert conversation.sent == []


async def test_nothing_due_before_the_send_time(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    await seed_edition(store, at(20))
    await approve_seeded(store, approved_at=at(24), send_at=at(25, 9))
    result = await run_periodic_check(config, conversation, store, clock(at(25, 8)))
    assert result.status == "nothing_due"
    assert conversation.sent == []


# send: timing and full effect


async def test_sends_at_the_approval_time_in_on_approval_mode(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    on_approval = config.model_copy(update={"send": SendConfig(mode="on_approval")})
    await seed_edition(store, at(20))
    await approve_seeded(store, approved_at=at(24, 14), send_at=at(24, 14))

    result = await run_periodic_check(on_approval, conversation, store, clock(at(24, 14)))

    assert result.status == "sent"
    assert len(conversation.sent) == 2  # the newsletter, then the reviewer notice
    to_all_staff = conversation.sent[0]
    assert to_all_staff.to == [config.all_staff]
    assert to_all_staff.reply_to == [config.mailboxes.submissions]
    assert "Review" not in to_all_staff.html
    assert "Priya Shah signed Northwind Retail" in to_all_staff.html

    notice = conversation.sent[1]
    assert notice.to == config.reviewers
    assert notice.subject.startswith("Sent:")

    edition = await store.open_edition()
    assert edition is None
    closed = await store.edition_for_build("b1")
    assert closed is not None
    assert closed.state == "sent"
    assert closed.sent_at == at(24, 14)
    assert closed.closed_at == at(24, 14)


async def test_sends_at_the_scheduled_time_not_before(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    # config.example.yaml sets send.mode: scheduled, day: MON, time: "09:00".
    await seed_edition(store, at(20))
    await approve_seeded(store, approved_at=at(21, 8), send_at=at(28, 8))  # 09:00 BST = 08:00 UTC

    before = await run_periodic_check(config, conversation, store, clock(at(28, 7, 59)))
    assert before.status == "nothing_due"

    after = await run_periodic_check(config, conversation, store, clock(at(28, 8)))
    assert after.status == "sent"


# send_started without sent


async def test_send_started_without_sent_returns_an_alert_and_does_not_resend(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    edition_id = await seed_edition(store, at(20))
    await approve_seeded(store, approved_at=at(24), send_at=at(24), send_started=True)

    result = await run_periodic_check(config, conversation, store, clock(at(25)))

    assert result.status == "send_alert"
    assert result.edition_id == edition_id
    assert conversation.sent == []
    reread = await store.open_edition()
    assert reread is not None and reread.state == "approved" and reread.send_started is True


# the approval re-check


async def test_re_check_refuses_when_approver_is_no_longer_a_reviewer(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    await seed_edition(store, at(20))
    approved = await approve_seeded(
        store, approver="left-the-company@techary.ai", approved_at=at(24), send_at=at(24)
    )

    result = await run_periodic_check(config, conversation, store, clock(at(25)))

    assert result.status == "refused"
    assert conversation.sent == []
    # Nothing changed: send_started was never recorded, so the edition stays approved for the
    # next check, exactly as a failure before send_started leaves it.
    reread = await store.open_edition()
    assert reread == approved


async def test_re_check_refuses_when_the_current_version_has_moved_on(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    await seed_edition(store, at(20))
    # Approve version 1, then simulate the current version having moved to 2 without a proper
    # withdrawal: a state the design's transitions never produce, guarded against here anyway.
    approved_at_v1 = await approve_seeded(
        store, approved_at=at(24), send_at=at(24), current_version=2
    )
    version_2 = Version(
        number=2,
        draft=DRAFT_V1,
        headline="A revised week",
        item_ids=["item-1"],
        check_results=[],
        creator=REVIEWER,
        created_at=at(21),
    )
    await store.add_version(approved_at_v1, version_2)

    result = await run_periodic_check(config, conversation, store, clock(at(25)))

    assert result.status == "refused"
    assert conversation.sent == []
    reread = await store.open_edition()
    assert reread is not None
    assert reread.state == "approved" and reread.send_started is False


# expiry


async def test_expiry_closes_the_edition_and_sets_closed_at(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    await seed_edition(store, at(1))
    expiring = config.model_copy(update={"edition": EditionConfig(expire_after_days=7)})

    result = await run_periodic_check(expiring, conversation, store, clock(at(8)))

    assert result.status == "expired"
    assert len(conversation.sent) == 1
    assert conversation.sent[0].subject.startswith("Closed unsent:")
    edition = await store.open_edition()
    assert edition is None
    closed = await store.edition_for_build("b1")
    assert closed is not None
    assert closed.state == "expired"
    assert closed.closed_at == at(8)


async def test_no_expiry_when_expire_after_days_is_unset(
    config: Config, store: EditionStore, conversation: FakeMailbox
) -> None:
    never_expires = config.model_copy(update={"edition": EditionConfig(expire_after_days=None)})
    await seed_edition(store, at(1))
    far_future = at(1) + timedelta(days=365)
    result = await run_periodic_check(never_expires, conversation, store, clock(far_future))
    assert result.status == "nothing_due"
    assert conversation.sent == []


# Only the periodic check may send to all staff: no other module may even read the address.

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "pulse"


def test_all_staff_appears_only_in_config_and_periodic() -> None:
    offenders = [
        path.relative_to(SRC)
        for path in SRC.rglob("*.py")
        if path.name not in {"config.py", "periodic.py"}
        and "all_staff" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"all_staff referenced outside config.py and periodic.py: {offenders}"
