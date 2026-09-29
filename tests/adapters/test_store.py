import sqlite3
import stat
from pathlib import Path

import pytest

from pulse.adapters.store import SqliteStore
from pulse.entities.errors import StoreError
from pulse.entities.lifecycle import abandon, present
from pulse.entities.store import HistoryRow
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
    await SqliteStore(path).add_newsletter(make_newsletter())

    await SqliteStore(path).initialise()

    assert await SqliteStore(path).get_open_newsletter() == make_newsletter()


async def test_no_open_newsletter(store: SqliteStore) -> None:
    assert await store.get_open_newsletter() is None


async def test_newsletter_round_trip(store: SqliteStore) -> None:
    newsletter = present(make_newsletter())

    await store.add_newsletter(newsletter)

    assert await store.get_open_newsletter() == newsletter


async def test_closed_newsletter_is_not_open(store: SqliteStore) -> None:
    await store.add_newsletter(abandon(make_newsletter(), REVIEWER, [REVIEWER], OPENED))

    assert await store.get_open_newsletter() is None


async def test_feedback_is_recorded_once_per_message(store: SqliteStore, tmp_path: Path) -> None:
    await store.add_newsletter(make_newsletter())

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
    await store.add_newsletter(make_newsletter())
    await store.add_newsletter(make_newsletter("n-2"))

    await store.append_history("n-1", "r01", b"first")
    await store.append_history("n-2", "r09", b"other newsletter")
    await store.append_history("n-1", "r02", b"second")

    assert await store.load_history("n-1") == [
        HistoryRow(message_id="r01", data=b"first"),
        HistoryRow(message_id="r02", data=b"second"),
    ]


async def test_empty_history(store: SqliteStore) -> None:
    assert await store.load_history("n-1") == []


async def test_failed_write_changes_nothing(store: SqliteStore) -> None:
    await store.add_newsletter(make_newsletter())

    with pytest.raises(StoreError):
        await store.add_newsletter(make_newsletter())

    assert await store.get_open_newsletter() == make_newsletter()


async def test_unreadable_database_raises_store_error(tmp_path: Path) -> None:
    path = tmp_path / "pulse.db"
    path.write_bytes(b"not a database" * 100)

    with pytest.raises(StoreError):
        await SqliteStore(path).initialise()
