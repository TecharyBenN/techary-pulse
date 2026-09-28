import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from pulse.errors import EditionStoreError
from pulse.models import Edition
from pulse.store import EditionStore, ItemRow

pytestmark = pytest.mark.anyio


def at(day: int) -> datetime:
    return datetime(2026, 9, day, 17, 30, tzinfo=UTC)


ITEMS = [
    ItemRow(
        item_id="item-1",
        kind="item",
        record={"category": "customer_win", "facts": ["Signed Northwind Retail"]},
        source_message_ids=["m01", "m02"],
    ),
    ItemRow(
        item_id="excluded-1",
        kind="excluded",
        record={"reason": "sensitivity"},
        source_message_ids=["m03"],
    ),
]
NOTE = [
    ModelRequest(parts=[UserPromptPart(content="Pulse note: version 1 was sent.")]),
]


async def create(store: EditionStore, build_id: str = "b1", **overrides: object) -> Edition:
    kwargs: dict[str, object] = dict(
        build_id=build_id,
        trigger="command",
        created_at=at(25),
        items=ITEMS,
        draft={"intro": "A good week.", "sections": []},
        headline="A new customer",
        included_item_ids=["item-1"],
        check_results=[],
        creator="build",
        history=NOTE,
    )
    kwargs.update(overrides)
    return await store.create_edition(**kwargs)  # type: ignore[arg-type]


async def test_create_then_read_back_through_open_and_for_build(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    assert created.build_id == "b1"
    assert created.state == "in_review"
    assert created.trigger == "command"
    assert created.current_version == 1

    opened = await store.open_edition()
    assert opened == created

    by_build = await store.edition_for_build("b1")
    assert by_build == created

    assert await store.edition_for_build("unknown") is None


async def test_duplicate_item_id_leaves_nothing_saved(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    duplicate = [*ITEMS, ItemRow("item-1", "item", {}, [])]

    with pytest.raises(EditionStoreError):
        await create(store, items=duplicate)

    assert await store.open_edition() is None
    assert await store.edition_for_build("b1") is None


async def test_closed_editions_older_than_cutoff_are_deleted_and_open_ones_kept(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "pulse.db"
    store = EditionStore(db_path)
    open_edition = await create(store, build_id="open")

    conn = sqlite3.connect(db_path)
    for edition_id, closed_at in (("old", at(1)), ("recent", at(24))):
        conn.execute(
            "INSERT INTO editions "
            "(id, build_id, state, trigger_, created_at, current_version, closed_at) "
            "VALUES (?, ?, 'sent', 'command', ?, 1, ?)",
            (edition_id, edition_id, at(1).isoformat(), closed_at.isoformat()),
        )
    conn.commit()
    conn.close()

    deleted = await store.delete_closed_before(at(14))

    assert deleted == ["old"]
    remaining = sqlite3.connect(db_path).execute("SELECT id FROM editions").fetchall()
    assert {row[0] for row in remaining} == {"recent", open_edition.id}


async def test_database_file_is_created_with_owner_only_permissions(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "pulse.db"
    store = EditionStore(db_path)
    await store.open_edition()

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
