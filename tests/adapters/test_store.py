import asyncio
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from pulse.adapters.store import SqliteStore
from pulse.entities.errors import StoreError
from pulse.entities.lifecycle import abandon, present, update
from pulse.entities.store import HistoryRow
from tests.emails import make_output, make_submission
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
        connection.execute("DROP TABLE submissions")

    with pytest.raises(StoreError):
        await store.save_start(make_newsletter(), [make_submission()])

    assert await store.get_open_newsletter() is None


async def test_unreadable_database_raises_store_error(tmp_path: Path) -> None:
    path = tmp_path / "pulse.db"
    path.write_bytes(b"not a database" * 100)

    with pytest.raises(StoreError):
        await SqliteStore(path).initialise()


async def test_start_saves_the_newsletter_and_its_submissions(store: SqliteStore) -> None:
    newsletter = make_newsletter()
    submissions = [make_submission("m02", received=OPENED), make_submission("m01")]

    await store.save_start(newsletter, submissions)

    assert await store.get_open_newsletter() == newsletter
    assert await store.list_submissions("n-1") == sorted(
        submissions, key=lambda s: (s.received, s.message_id)
    )


async def test_start_updates_the_newsletter_and_ignores_known_submissions(
    store: SqliteStore,
) -> None:
    await store.save_start(make_newsletter(), [make_submission("m01")])
    updated = update(make_newsletter(), OPENED + timedelta(days=1))

    await store.save_start(
        updated, [make_submission("m01", subject="Changed"), make_submission("m02")]
    )

    assert await store.get_open_newsletter() == updated
    listed = await store.list_submissions("n-1")
    assert [(s.message_id, s.subject) for s in listed] == [
        ("m01", "Signed Northwind Retail today"),
        ("m02", "Signed Northwind Retail today"),
    ]


async def test_submissions_belong_to_their_newsletter(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [make_submission("m01")])
    await store.save_start(make_newsletter("n-2"), [make_submission("m01")])

    assert [s.message_id for s in await store.list_submissions("n-2")] == ["m01"]


async def test_rejected_submission_round_trip(store: SqliteStore) -> None:
    rejected = make_submission("m13", sender_address="alex.morgan@example.com")
    await store.save_start(make_newsletter(), [rejected])

    assert await store.list_submissions("n-1") == [rejected]


async def test_extract_records_are_numbered_within_the_newsletter(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    await store.save_start(make_newsletter("n-2"), [])
    await store.save_extract("n-2", make_output("m09", category=None))

    first = await store.save_extract("n-1", make_output("m01", category=None))
    included = await store.save_extract("n-1", make_output("m02"))
    second = await store.save_extract("n-1", make_output("m03", category=None))

    assert (first.excluded_id, included.excluded_id, second.excluded_id) == (
        "excluded-1",
        None,
        "excluded-2",
    )
    assert await store.list_extract_records("n-1") == [first, included, second]


async def test_replaced_extract_record_keeps_its_excluded_id(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    await store.save_extract("n-1", make_output("m01", category=None))

    replaced = await store.save_extract("n-1", make_output("m01", is_update=False))

    assert (replaced.exclusion, replaced.excluded_id) == ("not_an_update", "excluded-1")
    assert await store.list_extract_records("n-1") == [replaced]


async def test_parallel_extracts_get_distinct_excluded_ids(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])

    records = await asyncio.gather(
        *(store.save_extract("n-1", make_output(f"m{n:02}", category=None)) for n in range(10))
    )

    assert sorted(r.excluded_id or "" for r in records) == sorted(
        f"excluded-{n}" for n in range(1, 11)
    )
