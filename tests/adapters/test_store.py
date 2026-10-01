import asyncio
import sqlite3
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pulse.adapters.store import SqliteStore
from pulse.entities.content import CheckFailure, Version, version_of
from pulse.entities.errors import StoreError
from pulse.entities.extracts import Sensitivity, restored
from pulse.entities.lifecycle import abandon, open_newsletter, present, update
from pulse.entities.store import HistoryRow
from tests.emails import (
    make_consolidation,
    make_draft,
    make_item,
    make_output,
    make_screened_email,
    make_verdict,
)
from tests.messages import OPENED, REVIEWER, make_message, make_newsletter

pytestmark = pytest.mark.anyio


@pytest.fixture
async def store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "state" / "pulse.db")
    await store.initialise()
    return store


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_initialise_creates_private_directory_and_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "pulse.db"

    await SqliteStore(path).initialise()

    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600


async def test_initialise_keeps_existing_data(tmp_path: Path) -> None:
    path = tmp_path / "pulse.db"
    await SqliteStore(path).initialise()
    await SqliteStore(path).save_start(make_newsletter(), [])

    await SqliteStore(path).initialise()

    assert await SqliteStore(path).get_open_newsletter() == make_newsletter()


async def test_no_open_newsletter(store: SqliteStore) -> None:
    assert await store.get_open_newsletter() is None


async def test_newsletter_round_trip(store: SqliteStore) -> None:
    newsletter = present(make_newsletter())

    await store.save_start(newsletter, [])

    assert await store.get_open_newsletter() == newsletter


async def test_closed_newsletter_is_not_open(store: SqliteStore) -> None:
    await store.save_start(abandon(make_newsletter(), REVIEWER, OPENED), [])

    assert await store.get_open_newsletter() is None


async def test_no_latest_newsletter(store: SqliteStore) -> None:
    assert await store.get_latest_newsletter() is None


async def test_latest_newsletter_is_the_one_opened_last_whether_open_or_closed(
    store: SqliteStore,
) -> None:
    earlier = abandon(make_newsletter("n-1"), REVIEWER, OPENED + timedelta(days=1))
    later = open_newsletter("n-2", OPENED + timedelta(days=2))
    await store.save_start(later, [])
    await store.save_start(earlier, [])

    assert await store.get_latest_newsletter() == later
    await store.save_newsletter(abandon(later, REVIEWER, OPENED + timedelta(days=3)))
    latest = await store.get_latest_newsletter()
    assert latest is not None and latest.newsletter_id == "n-2"


async def test_latest_newsletter_compares_times_not_text(store: SqliteStore) -> None:
    # 18:00 at UTC+05:00 is 13:00 UTC, earlier than 14:00 UTC, though it sorts later as text.
    earlier = open_newsletter(
        "n-1", datetime(2026, 9, 25, 18, 0, tzinfo=timezone(timedelta(hours=5)))
    )
    later = abandon(
        open_newsletter("n-2", datetime(2026, 9, 25, 14, 0, tzinfo=UTC)), REVIEWER, OPENED
    )
    await store.save_start(later, [])
    await store.save_start(abandon(earlier, REVIEWER, OPENED), [])

    latest = await store.get_latest_newsletter()
    assert latest is not None and latest.newsletter_id == "n-2"


async def test_save_newsletter_replaces_its_state(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    abandoned = abandon(make_newsletter(), REVIEWER, OPENED)

    await store.save_newsletter(abandoned)

    assert await store.get_open_newsletter() is None
    assert await store.get_latest_newsletter() == abandoned


async def test_mark_moved_records_the_move(store: SqliteStore) -> None:
    await store.save_start(
        make_newsletter(), [make_screened_email("m01"), make_screened_email("m02")]
    )

    await store.mark_moved("n-1", "m02")

    emails = await store.list_screened_emails("n-1")
    assert [(e.message_id, e.moved) for e in emails] == [("m01", False), ("m02", True)]


async def test_a_message_can_be_feedback_for_two_newsletters(store: SqliteStore) -> None:
    await store.record_feedback("n-1", make_message("r01"))
    await store.record_feedback("n-2", make_message("r01"))

    assert [m.message_id for m in await store.list_feedback("n-1")] == ["r01"]
    assert [m.message_id for m in await store.list_feedback("n-2")] == ["r01"]


async def test_feedback_is_recorded_once_per_message(store: SqliteStore, tmp_path: Path) -> None:
    await store.save_start(make_newsletter(), [])

    await store.record_feedback("n-1", make_message("r01"))
    await store.record_feedback("n-1", make_message("r01", text="A second copy"))
    await store.record_feedback("n-1", make_message("r02"))

    with sqlite3.connect(tmp_path / "state" / "pulse.db") as connection:
        rows = connection.execute("SELECT message_id, text FROM feedback ORDER BY message_id")
        assert rows.fetchall() == [
            ("r01", "Please make the intro shorter."),
            ("r02", "Please make the intro shorter."),
        ]


async def test_history_keeps_order_and_message_ids(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    await store.save_start(make_newsletter("n-2"), [])

    await store.append_history("n-1", "r01", b"first")
    await store.append_history("n-2", "r09", b"other newsletter")
    await store.append_history("n-1", "r02", b"second")

    assert await store.load_history("n-1") == [
        HistoryRow(message_id="r01", data=b"first"),
        HistoryRow(message_id="r02", data=b"second"),
    ]


async def test_empty_history(store: SqliteStore) -> None:
    assert await store.load_history("n-1") == []


async def test_failed_write_changes_nothing(store: SqliteStore, tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "state" / "pulse.db") as connection:
        connection.execute("DROP TABLE screened_emails")

    with pytest.raises(StoreError):
        await store.save_start(make_newsletter(), [make_screened_email()])

    assert await store.get_open_newsletter() is None


async def test_unreadable_database_raises_store_error(tmp_path: Path) -> None:
    path = tmp_path / "pulse.db"
    path.write_bytes(b"not a database" * 100)

    with pytest.raises(StoreError):
        await SqliteStore(path).initialise()


async def test_start_saves_the_newsletter_and_its_screened_emails(store: SqliteStore) -> None:
    newsletter = make_newsletter()
    emails = [make_screened_email("m02", received=OPENED), make_screened_email("m01")]

    await store.save_start(newsletter, emails)

    assert await store.get_open_newsletter() == newsletter
    assert await store.list_screened_emails("n-1") == sorted(
        emails, key=lambda s: (s.received, s.message_id)
    )


async def test_start_updates_the_newsletter_and_ignores_known_emails(
    store: SqliteStore,
) -> None:
    await store.save_start(make_newsletter(), [make_screened_email("m01")])
    updated = update(make_newsletter(), OPENED + timedelta(days=1))

    await store.save_start(
        updated, [make_screened_email("m01", subject="Changed"), make_screened_email("m02")]
    )

    assert await store.get_open_newsletter() == updated
    listed = await store.list_screened_emails("n-1")
    assert [(s.message_id, s.subject) for s in listed] == [
        ("m01", "Signed Northwind Retail today"),
        ("m02", "Signed Northwind Retail today"),
    ]


async def test_screened_emails_belong_to_their_newsletter(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [make_screened_email("m01")])
    await store.save_start(make_newsletter("n-2"), [make_screened_email("m01")])

    assert [s.message_id for s in await store.list_screened_emails("n-2")] == ["m01"]


async def test_rejected_screened_email_round_trip(store: SqliteStore) -> None:
    rejected = make_screened_email("m13", sender_address="alex.morgan@example.com")
    await store.save_start(make_newsletter(), [rejected])

    assert await store.list_screened_emails("n-1") == [rejected]


async def test_extract_records_are_numbered_within_the_newsletter(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    await store.save_start(make_newsletter("n-2"), [])
    await store.save_extract("n-2", "m09", make_output(category=None))

    first = await store.save_extract("n-1", "m01", make_output(category=None))
    included = await store.save_extract("n-1", "m02", make_output())
    second = await store.save_extract("n-1", "m03", make_output(category=None))

    assert (first.excluded_id, included.excluded_id, second.excluded_id) == (
        "excluded-1",
        None,
        "excluded-2",
    )
    assert await store.list_extract_records("n-1") == [first, included, second]


async def test_replaced_extract_record_keeps_its_excluded_id(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    await store.save_extract("n-1", "m01", make_output(category=None))

    flag = Sensitivity(type="commercial", evidence="mentions a contract value")
    replaced = await store.save_extract("n-1", "m01", make_output(sensitivity=[flag]))

    assert (replaced.exclusion, replaced.excluded_id) == ("sensitivity", "excluded-1")
    assert await store.list_extract_records("n-1") == [replaced]


async def test_restored_record_is_saved_in_place(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    first = await store.save_extract("n-1", "m01", make_output(category=None))
    second = await store.save_extract("n-1", "m02", make_output())
    record = restored([first], "excluded-1", REVIEWER)

    await store.save_restored("n-1", record)

    assert await store.list_extract_records("n-1") == [record, second]
    # Extracting it again keeps the restore.
    again = await store.save_extract("n-1", "m01", make_output(category=None))
    assert (again.exclusion, again.restored_by) == (None, REVIEWER)


async def test_no_items_before_consolidation(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])

    assert await store.get_items("n-1") is None


async def test_saved_items_replace_earlier_items(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    await store.save_start(make_newsletter("n-2"), [])
    other = make_consolidation(make_item(source_message_ids=["m09"]))
    await store.save_items("n-2", other)
    await store.save_items("n-1", make_consolidation(make_item()))

    replacement = make_consolidation(make_item(), make_item("item-2"))
    await store.save_items("n-1", replacement)

    assert await store.get_items("n-1") == replacement
    assert await store.get_items("n-2") == other


async def test_parallel_extracts_get_distinct_excluded_ids(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])

    records = await asyncio.gather(
        *(store.save_extract("n-1", f"m{n:02}", make_output(category=None)) for n in range(10))
    )

    assert sorted(r.excluded_id or "" for r in records) == sorted(
        f"excluded-{n}" for n in range(1, 11)
    )


def _version(number: int) -> Version:
    failure = CheckFailure(check="dashes", target="intro", detail="em or en dash")
    return version_of(make_draft(), number, OPENED, [failure], [make_verdict(claim="Made up")])


async def test_no_draft_before_writing(store: SqliteStore) -> None:
    assert await store.get_draft("n-1") is None


async def test_saved_draft_replaces_the_earlier_draft(store: SqliteStore) -> None:
    await store.save_draft("n-1", make_draft())
    revised = make_draft(changes=["Shortened the intro"])

    await store.save_draft("n-1", revised)

    assert await store.get_draft("n-1") == revised
    assert await store.get_draft("n-2") is None


async def test_verdicts_belong_to_the_working_draft_they_judged(store: SqliteStore) -> None:
    await store.save_draft("n-1", make_draft())
    assert await store.get_verdicts("n-1") is None
    verdicts = [make_verdict("intro"), make_verdict(claim="Signed two customers")]

    await store.save_verdicts("n-1", verdicts)

    assert await store.get_verdicts("n-1") == verdicts
    assert await store.get_draft("n-1") == make_draft()
    # A new working draft has not been judged.
    await store.save_draft("n-1", make_draft(changes=["Shortened the intro"]))
    assert await store.get_verdicts("n-1") is None


async def test_version_is_saved_with_the_newsletter_that_numbers_it(store: SqliteStore) -> None:
    newsletter = make_newsletter()
    await store.save_start(newsletter, [])

    await store.save_version(present(newsletter), _version(1))

    saved = await store.get_open_newsletter()
    assert saved is not None
    assert saved.latest_version == 1
    assert await store.get_version("n-1", 1) == _version(1)
    assert await store.get_version("n-1", 2) is None


async def test_a_version_number_is_never_reused(store: SqliteStore) -> None:
    newsletter = present(make_newsletter())
    await store.save_version(newsletter, _version(1))
    moved_on = newsletter.model_copy(update={"updated_at": OPENED + timedelta(hours=1)})

    with pytest.raises(StoreError):
        await store.save_version(moved_on, _version(1))

    # The failed save changed nothing, the newsletter included.
    assert await store.get_open_newsletter() == newsletter


async def test_feedback_is_listed_in_the_order_recorded(store: SqliteStore) -> None:
    await store.record_feedback("n-1", make_message("r02", text="Second"))
    await store.record_feedback("n-1", make_message("r01", text="First"))
    await store.record_feedback("n-2", make_message("r03"))

    feedback = await store.list_feedback("n-1")

    assert [(m.message_id, m.text) for m in feedback] == [("r02", "Second"), ("r01", "First")]


async def test_unseen_message_is_not_handled(store: SqliteStore) -> None:
    assert await store.get_handled_message("c01") is None


async def test_attempts_are_counted_per_message(store: SqliteStore) -> None:
    assert await store.record_attempt("c01") == 1
    assert await store.record_attempt("c01") == 2
    assert await store.record_attempt("c02") == 1

    handled = await store.get_handled_message("c01")
    assert handled is not None
    assert (handled.attempts, handled.handled) == (2, False)


async def test_marking_handled_keeps_the_attempts(store: SqliteStore) -> None:
    await store.record_attempt("c01")

    await store.mark_handled("c01")

    handled = await store.get_handled_message("c01")
    assert handled is not None
    assert (handled.attempts, handled.handled) == (1, True)
