"""The edition store: a SQLite database holding editions, their versions, items and history."""

import asyncio
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from pulse.errors import EditionStoreError
from pulse.models import Edition, Trigger

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
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id TEXT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
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


def _row_to_edition(row: sqlite3.Row) -> Edition:
    return Edition(
        id=row["id"],
        build_id=row["build_id"],
        state=row["state"],
        trigger=row["trigger_"],
        created_at=datetime.fromisoformat(row["created_at"]),
        current_version=row["current_version"],
    )


class EditionStore:
    """The edition store at ``db_path``. Every write is one transaction."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        # Following state.py's RunArtefacts: the directory is 0700, and, since sqlite3.connect
        # creates the file subject to the process umask, its mode is fixed up on first creation.
        self._db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        created = not self._db_path.exists()
        # autocommit=False gives PEP 249-style transactions, so ``with conn:`` around a write
        # commits it as one transaction, or rolls it all back if a statement raises.
        conn = sqlite3.connect(self._db_path, autocommit=False)
        if created:
            os.chmod(self._db_path, 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _open_edition(self) -> Edition | None:
        conn = self._connect()
        try:
            placeholders = ", ".join("?" * len(_OPEN_STATES))
            row = conn.execute(
                f"SELECT * FROM editions WHERE state IN ({placeholders})", _OPEN_STATES
            ).fetchone()
            return None if row is None else _row_to_edition(row)
        finally:
            conn.close()

    async def open_edition(self) -> Edition | None:
        """Return the one open edition (``in_review`` or ``approved``), or None."""
        return await asyncio.to_thread(self._open_edition)

    def _edition_for_build(self, build_id: str) -> Edition | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM editions WHERE build_id = ?", (build_id,)).fetchone()
            return None if row is None else _row_to_edition(row)
        finally:
            conn.close()

    async def edition_for_build(self, build_id: str) -> Edition | None:
        """Return the edition created by ``build_id``, or None if that build made no edition."""
        return await asyncio.to_thread(self._edition_for_build, build_id)

    def _create_edition(
        self,
        *,
        build_id: str,
        trigger: Trigger,
        created_at: datetime,
        items: Sequence[ItemRow],
        draft: Mapping[str, Any],
        headline: str,
        included_item_ids: Sequence[str],
        check_results: Sequence[str],
        creator: str,
        history: Sequence[ModelMessage],
    ) -> Edition:
        edition_id = str(uuid.uuid4())
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO editions "
                    "(id, build_id, state, trigger_, created_at, current_version) "
                    "VALUES (?, ?, 'in_review', ?, ?, 1)",
                    (edition_id, build_id, trigger, created_at.isoformat()),
                )
                for item in items:
                    conn.execute(
                        "INSERT INTO items "
                        "(edition_id, item_id, kind, record, source_message_ids) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            edition_id,
                            item.item_id,
                            item.kind,
                            json.dumps(dict(item.record)),
                            json.dumps(list(item.source_message_ids)),
                        ),
                    )
                conn.execute(
                    "INSERT INTO versions "
                    "(edition_id, version, draft, headline, included_item_ids, check_results, "
                    "creator, created_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                    (
                        edition_id,
                        json.dumps(dict(draft)),
                        headline,
                        json.dumps(list(included_item_ids)),
                        json.dumps(list(check_results)),
                        creator,
                        created_at.isoformat(),
                    ),
                )
                if history:
                    payload = ModelMessagesTypeAdapter.dump_json(list(history)).decode()
                    conn.execute(
                        "INSERT INTO messages (edition_id, payload) VALUES (?, ?)",
                        (edition_id, payload),
                    )
        except sqlite3.IntegrityError as exc:
            raise EditionStoreError(
                f"could not create edition for build {build_id}: {exc}"
            ) from exc
        finally:
            conn.close()
        return Edition(
            id=edition_id,
            build_id=build_id,
            state="in_review",
            trigger=trigger,
            created_at=created_at,
            current_version=1,
        )

    async def create_edition(
        self,
        *,
        build_id: str,
        trigger: Trigger,
        created_at: datetime,
        items: Sequence[ItemRow],
        draft: Mapping[str, Any],
        headline: str,
        included_item_ids: Sequence[str],
        check_results: Sequence[str],
        creator: str,
        history: Sequence[ModelMessage] = (),
    ) -> Edition:
        """Create an edition with its items and version 1, in one transaction.

        Raises:
            EditionStoreError: If the write fails, such as a duplicate item ID; nothing is saved.
        """
        return await asyncio.to_thread(
            self._create_edition,
            build_id=build_id,
            trigger=trigger,
            created_at=created_at,
            items=items,
            draft=draft,
            headline=headline,
            included_item_ids=included_item_ids,
            check_results=check_results,
            creator=creator,
            history=history,
        )

    def _delete_closed_before(self, cutoff: datetime) -> list[str]:
        conn = self._connect()
        try:
            placeholders = ", ".join("?" * len(_CLOSED_STATES))
            with conn:
                rows = conn.execute(
                    f"SELECT id FROM editions WHERE state IN ({placeholders}) AND closed_at < ?",
                    (*_CLOSED_STATES, cutoff.isoformat()),
                ).fetchall()
                ids = [row["id"] for row in rows]
                conn.executemany("DELETE FROM editions WHERE id = ?", [(i,) for i in ids])
            return ids
        finally:
            conn.close()

    async def delete_closed_before(self, cutoff: datetime) -> list[str]:
        """Delete closed editions (sent, expired or discarded) older than ``cutoff``."""
        return await asyncio.to_thread(self._delete_closed_before, cutoff)


__all__ = ["EditionStore", "ItemRow"]
