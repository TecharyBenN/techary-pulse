import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from pulse.errors import EditionStoreError
from pulse.models import Draft, Edition, Feedback, NotApplied, Version
from pulse.store import EditionStore, ItemRow, SourceRow

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
SOURCES = [
    SourceRow(
        message_id="m01",
        outcome="included",
        subject="Signed Northwind Retail",
        sender_name="Priya Shah",
        sender_address="priya.shah@techary.ai",
        received_at=at(20),
        body="We signed Northwind Retail.",
        has_attachments=False,
    ),
    SourceRow(
        message_id="m03",
        outcome="excluded",
        subject="Confidential margins",
        sender_name="Tom Evans",
        sender_address="tom.evans@techary.ai",
        received_at=at(20),
        body="Our margin on this deal is 40%.",
        has_attachments=True,
    ),
    SourceRow(message_id="m07", outcome="rejected", subject="Out of office"),
]
NOTE = [
    ModelRequest(parts=[UserPromptPart(content="Pulse note: version 1 was sent.")]),
]


VERSION_1 = Version(
    number=1,
    draft=Draft.model_validate({"intro": "A good week.", "sections": []}),
    headline="A new customer",
    item_ids=["item-1"],
    check_results=[],
    creator="build",
    created_at=at(25),
)


async def create(
    store: EditionStore, build_id: str = "b1", items: list[ItemRow] = ITEMS
) -> Edition:
    return await store.create_edition(
        build_id=build_id,
        trigger="command",
        items=items,
        sources=SOURCES,
        version=VERSION_1,
        history=NOTE,
    )


async def test_create_then_read_back_through_open_and_for_build(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    assert created.build_id == "b1"
    assert created.state == "in_review"
    assert created.trigger == "command"
    assert created.current_version == 1
    assert created.approved_version is None
    assert created.send_started is False

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


# sources


async def test_sources_round_trip(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    rows = await store.sources(created.id)
    assert list(rows) == ["m01", "m03", "m07"], "sources come back in the order they were saved"
    assert rows["m01"].outcome == "included"
    assert rows["m01"].sender_name == "Priya Shah"
    assert rows["m01"].body == "We signed Northwind Retail."
    assert rows["m01"].has_attachments is False
    assert rows["m03"].outcome == "excluded"
    assert rows["m03"].has_attachments is True


async def test_rejected_sources_have_no_body_or_sender(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    rejected = next(
        r for r in (await store.sources(created.id)).values() if r.outcome == "rejected"
    )
    assert rejected.subject == "Out of office"
    assert rejected.body is None
    assert rejected.sender_name is None
    assert rejected.sender_address is None
    assert rejected.received_at is None


# items


async def test_items_round_trip(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    items = await store.items(created.id)
    assert [row.item_id for row in items] == ["item-1", "excluded-1"], "items keep build order"
    rows = {row.item_id: row for row in items}
    assert rows["item-1"].kind == "item"
    assert rows["item-1"].source_message_ids == ["m01", "m02"]
    assert rows["excluded-1"].kind == "excluded"
    assert rows["excluded-1"].record == {"reason": "sensitivity"}


# versions: current_version, version, add_version


async def test_current_version_and_version_read_back_version_1(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    version = await store.version(created.id, 1)
    assert version.number == 1
    assert version.headline == "A new customer"
    assert version.draft == Draft.model_validate({"intro": "A good week.", "sections": []})
    assert version.item_ids == ["item-1"]
    assert version.changes == []
    assert version.not_applied == []
    assert version.creator == "build"

    assert await store.current_version(created.id) == version


async def test_version_raises_for_an_unknown_version(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)
    with pytest.raises(EditionStoreError):
        await store.version(created.id, 2)


async def test_add_version_saves_the_version_and_updates_the_edition(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    new_edition = created.model_copy(update={"current_version": 2})
    new = Version(
        number=2,
        draft=Draft.model_validate({"intro": "A revised week.", "sections": []}),
        headline="A revised headline",
        item_ids=["item-1"],
        check_results=[],
        changes=["Shortened the intro"],
        not_applied=[NotApplied(feedback="Add the contract value", reason="excluded")],
        creator="reviewer@techary.ai",
        created_at=at(26),
    )
    await store.add_version(new_edition, new)

    stored = await store.version(created.id, 2)
    assert stored == new
    assert (await store.current_version(created.id)) == new
    reread = await store.edition_for_build("b1")
    assert reread is not None and reread.current_version == 2


# edition transitions: update_edition


async def test_update_edition_persists_every_mutable_field(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    approved = created.model_copy(
        update={
            "state": "approved",
            "approved_version": 1,
            "approver": "reviewer@techary.ai",
            "approved_at": at(26),
            "send_at": at(27),
        }
    )
    await store.update_edition(approved)
    assert await store.open_edition() == approved

    sent = approved.model_copy(
        update={
            "state": "sent",
            "send_started": True,
            "sent_at": at(28),
            "closed_at": at(28),
        }
    )
    await store.update_edition(sent)
    assert await store.open_edition() is None
    assert await store.edition_for_build("b1") == sent


# feedback


async def test_feedback_round_trip_in_received_order(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    first = Feedback(
        reviewer="a@techary.ai", channel="email", text="Add more detail", received_at=at(26)
    )
    second = Feedback(
        reviewer="b@techary.ai", channel="librechat", text="Looks good", received_at=at(27)
    )
    await store.save_turn(created.id, [], first)
    await store.save_turn(created.id, [], second)

    assert await store.feedback(created.id) == [first, second]


# load_history


async def test_load_history_concatenates_saved_turns_in_order(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    turn_one = [ModelResponse(parts=[TextPart("Here is version 1.")])]
    turn_two = [ModelRequest(parts=[UserPromptPart(content="Thanks")])]
    await store.save_turn(
        created.id,
        turn_one,
        Feedback(reviewer="a@techary.ai", channel="email", text="x", received_at=at(26)),
    )
    await store.save_turn(
        created.id,
        turn_two,
        Feedback(reviewer="a@techary.ai", channel="email", text="y", received_at=at(27)),
    )

    history = await store.load_history(created.id)
    assert len(history) == len(NOTE) + len(turn_one) + len(turn_two)
    assert history[-2:] == [*turn_one, *turn_two]


# save_turn atomicity


class _FailingOnHandledMessages(sqlite3.Connection):
    """A connection that fails the last statement `_save_turn` issues, to test rollback."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.startswith("INSERT INTO handled_messages"):
            raise sqlite3.OperationalError("boom")
        return super().execute(sql, *args)


async def test_save_turn_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    real_connect = sqlite3.connect

    def failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = _FailingOnHandledMessages
        connection: sqlite3.Connection = real_connect(*args, **kwargs)
        return connection

    monkeypatch.setattr(sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError):
        await store.save_turn(
            created.id,
            [ModelResponse(parts=[TextPart("hello")])],
            Feedback(reviewer="a@techary.ai", channel="email", text="x", received_at=at(26)),
            handled_message_id="msg-1",
        )

    monkeypatch.undo()
    assert await store.feedback(created.id) == []
    assert await store.load_history(created.id) == NOTE
    assert await store.is_handled("msg-1") is False


# handled_messages: record_attempt, is_handled


async def test_record_attempt_counts_up_from_one(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    assert await store.record_attempt("msg-1") == 1
    assert await store.record_attempt("msg-1") == 2
    assert await store.record_attempt("msg-1") == 3
    assert await store.record_attempt("msg-2") == 1


async def test_is_handled_defaults_false_then_true_after_save_turn(tmp_path: Path) -> None:
    store = EditionStore(tmp_path / "state" / "pulse.db")
    created = await create(store)

    assert await store.is_handled("msg-1") is False
    await store.record_attempt("msg-1")
    assert await store.is_handled("msg-1") is False

    await store.save_turn(
        created.id,
        [],
        Feedback(reviewer="a@techary.ai", channel="email", text="x", received_at=at(26)),
        handled_message_id="msg-1",
    )
    assert await store.is_handled("msg-1") is True
