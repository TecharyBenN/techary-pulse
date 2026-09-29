"""The edition store: a SQLite database holding editions, their versions, items and history."""

import asyncio
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from pulse.errors import EditionStoreError
from pulse.models import Draft, Edition, ExtractRecord, Feedback, NotApplied, Trigger, Version

_SCHEMA = """
CREATE TABLE IF NOT EXISTS editions (
    id TEXT PRIMARY KEY,
    build_id TEXT NOT NULL,
    state TEXT NOT NULL,
    trigger_ TEXT NOT NULL,
    created_at TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    approved_version INTEGER,
    approver TEXT,
    approved_at TEXT,
    send_at TEXT,
    send_started INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    edition_id TEXT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    draft TEXT NOT NULL,
    headline TEXT NOT NULL,
    included_item_ids TEXT NOT NULL,
    check_results TEXT NOT NULL,
    changes TEXT NOT NULL DEFAULT '[]',
    not_applied TEXT NOT NULL DEFAULT '[]',
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (edition_id, version)
);
CREATE TABLE IF NOT EXISTS items (
    edition_id TEXT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    record TEXT NOT NULL,
    source_message_ids TEXT NOT NULL,
    PRIMARY KEY (edition_id, item_id)
);
CREATE TABLE IF NOT EXISTS sources (
    edition_id TEXT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    subject TEXT NOT NULL,
    sender_name TEXT,
    sender_address TEXT,
    received_at TEXT,
    body TEXT,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (edition_id, message_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id TEXT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    reviewer TEXT NOT NULL,
    channel TEXT NOT NULL,
    text TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id TEXT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handled_messages (
    message_id TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    handled INTEGER NOT NULL DEFAULT 0
);
"""

_OPEN_STATES = ("in_review", "approved")
_CLOSED_STATES = ("sent", "expired", "discarded")


@dataclass(frozen=True)
class ItemRow:
    """One row of the ``items`` table: a consolidated item or an excluded record."""

    item_id: str
    kind: Literal["item", "excluded"]
    record: Mapping[str, Any]
    source_message_ids: Sequence[str]

    def excluded(self) -> tuple[ExtractRecord, str]:
        """An excluded record's extract record and exclusion reason."""
        fields = {k: v for k, v in self.record.items() if k != "reason"}
        return ExtractRecord.model_validate(fields), str(self.record["reason"])


@dataclass(frozen=True)
class SourceRow:
    """One snapshot message from a build, with its outcome.

    A rejected message keeps only its subject, so content from a disallowed sender or label
    is never stored.
    """

    message_id: str
    outcome: Literal["included", "excluded", "rejected"]
    subject: str
    sender_name: str | None = None
    sender_address: str | None = None
    received_at: datetime | None = None
    body: str | None = None
    has_attachments: bool = False


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_edition(row: sqlite3.Row) -> Edition:
    return Edition(
        id=row["id"],
        build_id=row["build_id"],
        state=row["state"],
        trigger=row["trigger_"],
        created_at=datetime.fromisoformat(row["created_at"]),
        current_version=row["current_version"],
        approved_version=row["approved_version"],
        approver=row["approver"],
        approved_at=_datetime(row["approved_at"]),
        send_at=_datetime(row["send_at"]),
        send_started=bool(row["send_started"]),
        sent_at=_datetime(row["sent_at"]),
        closed_at=_datetime(row["closed_at"]),
    )


def _write_edition(conn: sqlite3.Connection, edition: Edition) -> None:
    """Write every mutable field of ``edition`` to its existing row."""
    conn.execute(
        "UPDATE editions SET state = ?, current_version = ?, approved_version = ?, approver = ?, "
        "approved_at = ?, send_at = ?, send_started = ?, sent_at = ?, closed_at = ? WHERE id = ?",
        (
            edition.state,
            edition.current_version,
            edition.approved_version,
            edition.approver,
            _iso(edition.approved_at),
            _iso(edition.send_at),
            int(edition.send_started),
            _iso(edition.sent_at),
            _iso(edition.closed_at),
            edition.id,
        ),
    )


def _insert_version(conn: sqlite3.Connection, edition_id: str, version: Version) -> None:
    conn.execute(
        "INSERT INTO versions "
        "(edition_id, version, draft, headline, included_item_ids, check_results, "
        "changes, not_applied, creator, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edition_id,
            version.number,
            version.draft.model_dump_json(),
            version.headline,
            json.dumps(version.item_ids),
            json.dumps(version.check_results),
            json.dumps(version.changes),
            json.dumps([n.model_dump(mode="json") for n in version.not_applied]),
            version.creator,
            version.created_at.isoformat(),
        ),
    )


def _select_version(conn: sqlite3.Connection, edition_id: str, number: int) -> Version:
    row = conn.execute(
        "SELECT * FROM versions WHERE edition_id = ? AND version = ?", (edition_id, number)
    ).fetchone()
    if row is None:
        raise EditionStoreError(f"no version {number} for edition {edition_id}")
    return Version(
        number=row["version"],
        draft=Draft.model_validate_json(row["draft"]),
        headline=row["headline"],
        item_ids=json.loads(row["included_item_ids"]),
        check_results=json.loads(row["check_results"]),
        changes=json.loads(row["changes"]),
        not_applied=[NotApplied.model_validate(n) for n in json.loads(row["not_applied"])],
        creator=row["creator"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _append_history(
    conn: sqlite3.Connection, edition_id: str, messages: Sequence[ModelMessage]
) -> None:
    if messages:
        payload = ModelMessagesTypeAdapter.dump_json(list(messages)).decode()
        conn.execute(
            "INSERT INTO messages (edition_id, payload) VALUES (?, ?)", (edition_id, payload)
        )


class EditionStore:
    """The edition store at ``db_path``. Every write is one transaction."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # Following state.py's _write: the directory is 0700, and the file, if absent, is
        # created with 0600 directly, so it never exists at a wider mode even momentarily.
        self._db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._db_path.exists():
            os.close(os.open(self._db_path, os.O_CREAT | os.O_WRONLY, 0o600))
        # autocommit=False gives PEP 249-style transactions, so ``with conn:`` around a write
        # commits it as one transaction, or rolls it all back if a statement raises.
        conn = sqlite3.connect(self._db_path, autocommit=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(_SCHEMA)
            conn.commit()
            yield conn
        finally:
            conn.close()

    def _open_edition(self) -> Edition | None:
        with self._connection() as conn:
            placeholders = ", ".join("?" * len(_OPEN_STATES))
            row = conn.execute(
                f"SELECT * FROM editions WHERE state IN ({placeholders})", _OPEN_STATES
            ).fetchone()
            return None if row is None else _row_to_edition(row)

    async def open_edition(self) -> Edition | None:
        """Return the one open edition (``in_review`` or ``approved``), or None."""
        return await asyncio.to_thread(self._open_edition)

    def _edition_for_build(self, build_id: str) -> Edition | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM editions WHERE build_id = ?", (build_id,)).fetchone()
            return None if row is None else _row_to_edition(row)

    async def edition_for_build(self, build_id: str) -> Edition | None:
        """Return the edition created by ``build_id``, or None if that build made no edition."""
        return await asyncio.to_thread(self._edition_for_build, build_id)

    def _create_edition(
        self,
        build_id: str,
        trigger: Trigger,
        items: Sequence[ItemRow],
        sources: Sequence[SourceRow],
        version: Version,
        history: Sequence[ModelMessage],
    ) -> Edition:
        edition = Edition(
            id=str(uuid.uuid4()),
            build_id=build_id,
            state="in_review",
            trigger=trigger,
            created_at=version.created_at,
            current_version=version.number,
        )
        try:
            with self._connection() as conn, conn:
                conn.execute(
                    "INSERT INTO editions "
                    "(id, build_id, state, trigger_, created_at, current_version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        edition.id,
                        build_id,
                        edition.state,
                        trigger,
                        edition.created_at.isoformat(),
                        edition.current_version,
                    ),
                )
                conn.executemany(
                    "INSERT INTO items (edition_id, item_id, kind, record, source_message_ids) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            edition.id,
                            item.item_id,
                            item.kind,
                            json.dumps(dict(item.record)),
                            json.dumps(list(item.source_message_ids)),
                        )
                        for item in items
                    ],
                )
                conn.executemany(
                    "INSERT INTO sources (edition_id, message_id, outcome, subject, "
                    "sender_name, sender_address, received_at, body, has_attachments) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            edition.id,
                            source.message_id,
                            source.outcome,
                            source.subject,
                            source.sender_name,
                            source.sender_address,
                            _iso(source.received_at),
                            source.body,
                            int(source.has_attachments),
                        )
                        for source in sources
                    ],
                )
                _insert_version(conn, edition.id, version)
                _append_history(conn, edition.id, history)
        except sqlite3.IntegrityError as exc:
            raise EditionStoreError(
                f"could not create edition for build {build_id}: {exc}"
            ) from exc
        return edition

    async def create_edition(
        self,
        *,
        build_id: str,
        trigger: Trigger,
        items: Sequence[ItemRow],
        sources: Sequence[SourceRow],
        version: Version,
        history: Sequence[ModelMessage] = (),
    ) -> Edition:
        """Create an edition with its items, sources and first version, in one transaction.

        The edition's creation time is the version's.

        Raises:
            EditionStoreError: If the write fails, such as a duplicate item ID; nothing is saved.
        """
        return await asyncio.to_thread(
            self._create_edition, build_id, trigger, items, sources, version, history
        )

    def _update_edition(self, edition: Edition) -> None:
        with self._connection() as conn, conn:
            _write_edition(conn, edition)

    async def update_edition(self, edition: Edition) -> None:
        """Persist every mutable field of ``edition``: its state and transition fields."""
        await asyncio.to_thread(self._update_edition, edition)

    def _add_version(self, edition: Edition, version: Version) -> None:
        try:
            with self._connection() as conn, conn:
                _insert_version(conn, edition.id, version)
                _write_edition(conn, edition)
        except sqlite3.IntegrityError as exc:
            raise EditionStoreError(
                f"could not add version {version.number} to edition {edition.id}: {exc}"
            ) from exc

    async def add_version(self, edition: Edition, version: Version) -> None:
        """Save ``version`` and update the edition's mutable fields together, one transaction."""
        await asyncio.to_thread(self._add_version, edition, version)

    def _version(self, edition_id: str, number: int | None) -> Version:
        with self._connection() as conn:
            if number is None:
                row = conn.execute(
                    "SELECT current_version FROM editions WHERE id = ?", (edition_id,)
                ).fetchone()
                if row is None:
                    raise EditionStoreError(f"no edition {edition_id}")
                number = int(row["current_version"])
            return _select_version(conn, edition_id, number)

    async def version(self, edition_id: str, number: int) -> Version:
        """Return one version of an edition.

        Raises:
            EditionStoreError: If the edition has no such version.
        """
        return await asyncio.to_thread(self._version, edition_id, number)

    async def current_version(self, edition_id: str) -> Version:
        """Return an edition's current version.

        Raises:
            EditionStoreError: If the edition, or its current version, does not exist.
        """
        return await asyncio.to_thread(self._version, edition_id, None)

    def _items(self, edition_id: str) -> list[ItemRow]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE edition_id = ? ORDER BY rowid", (edition_id,)
            ).fetchall()
        return [
            ItemRow(
                item_id=row["item_id"],
                kind=row["kind"],
                record=json.loads(row["record"]),
                source_message_ids=json.loads(row["source_message_ids"]),
            )
            for row in rows
        ]

    async def items(self, edition_id: str) -> list[ItemRow]:
        """Return an edition's consolidated items and excluded records, in build order."""
        return await asyncio.to_thread(self._items, edition_id)

    def _sources(self, edition_id: str) -> dict[str, SourceRow]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE edition_id = ? ORDER BY rowid", (edition_id,)
            ).fetchall()
        return {
            row["message_id"]: SourceRow(
                message_id=row["message_id"],
                outcome=row["outcome"],
                subject=row["subject"],
                sender_name=row["sender_name"],
                sender_address=row["sender_address"],
                received_at=_datetime(row["received_at"]),
                body=row["body"],
                has_attachments=bool(row["has_attachments"]),
            )
            for row in rows
        }

    async def sources(self, edition_id: str) -> dict[str, SourceRow]:
        """Return an edition build's snapshot messages by message ID, in received order."""
        return await asyncio.to_thread(self._sources, edition_id)

    def _feedback(self, edition_id: str) -> list[Feedback]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE edition_id = ? ORDER BY id", (edition_id,)
            ).fetchall()
        return [
            Feedback(
                reviewer=row["reviewer"],
                channel=row["channel"],
                text=row["text"],
                received_at=datetime.fromisoformat(row["received_at"]),
            )
            for row in rows
        ]

    async def feedback(self, edition_id: str) -> list[Feedback]:
        """Return every reviewer message recorded against an edition, in received order."""
        return await asyncio.to_thread(self._feedback, edition_id)

    def _load_history(self, edition_id: str) -> list[ModelMessage]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM messages WHERE edition_id = ? ORDER BY id", (edition_id,)
            ).fetchall()
        return [m for row in rows for m in ModelMessagesTypeAdapter.validate_json(row["payload"])]

    async def load_history(self, edition_id: str) -> list[ModelMessage]:
        """Return an edition's conversation history, in order."""
        return await asyncio.to_thread(self._load_history, edition_id)

    def _record_attempt(self, message_id: str) -> int:
        with self._connection() as conn, conn:
            conn.execute(
                "INSERT INTO handled_messages (message_id, attempts, handled) VALUES (?, 1, 0) "
                "ON CONFLICT(message_id) DO UPDATE SET attempts = attempts + 1",
                (message_id,),
            )
            row = conn.execute(
                "SELECT attempts FROM handled_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            return int(row["attempts"])

    async def record_attempt(self, message_id: str) -> int:
        """Record an attempt to handle a conversation mailbox message; return the new count."""
        return await asyncio.to_thread(self._record_attempt, message_id)

    def _is_handled(self, message_id: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT handled FROM handled_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            return row is not None and bool(row["handled"])

    async def is_handled(self, message_id: str) -> bool:
        """Whether a conversation mailbox message has already been handled."""
        return await asyncio.to_thread(self._is_handled, message_id)

    def _save_turn(
        self,
        edition_id: str,
        new_messages: Sequence[ModelMessage],
        feedback: Feedback,
        handled_message_id: str | None,
    ) -> None:
        with self._connection() as conn, conn:
            _append_history(conn, edition_id, new_messages)
            conn.execute(
                "INSERT INTO feedback (edition_id, reviewer, channel, text, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    edition_id,
                    feedback.reviewer,
                    feedback.channel,
                    feedback.text,
                    feedback.received_at.isoformat(),
                ),
            )
            if handled_message_id is not None:
                conn.execute(
                    "INSERT INTO handled_messages (message_id, attempts, handled) "
                    "VALUES (?, 1, 1) ON CONFLICT(message_id) DO UPDATE SET handled = 1",
                    (handled_message_id,),
                )

    async def save_turn(
        self,
        edition_id: str,
        new_messages: Sequence[ModelMessage],
        feedback: Feedback,
        handled_message_id: str | None = None,
    ) -> None:
        """Save a chat agent run's new history, the reviewer's feedback and, in the email
        channel, that the polled message is handled, in one transaction.
        """
        await asyncio.to_thread(
            self._save_turn, edition_id, new_messages, feedback, handled_message_id
        )

    def _delete_closed_before(self, cutoff: datetime) -> list[str]:
        with self._connection() as conn, conn:
            placeholders = ", ".join("?" * len(_CLOSED_STATES))
            rows = conn.execute(
                f"SELECT id FROM editions WHERE state IN ({placeholders}) AND closed_at < ?",
                (*_CLOSED_STATES, cutoff.isoformat()),
            ).fetchall()
            ids = [row["id"] for row in rows]
            conn.executemany("DELETE FROM editions WHERE id = ?", [(i,) for i in ids])
            return ids

    async def delete_closed_before(self, cutoff: datetime) -> list[str]:
        """Delete closed editions (sent, expired or discarded) older than ``cutoff``."""
        return await asyncio.to_thread(self._delete_closed_before, cutoff)


__all__ = ["EditionStore", "ItemRow", "SourceRow"]
