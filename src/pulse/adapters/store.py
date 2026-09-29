"""The SQLite store."""

import asyncio
import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path

from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import StoreError
from pulse.entities.lifecycle import CLOSED_STATES, Newsletter
from pulse.entities.store import HistoryRow

_NEWSLETTER_COLUMNS = tuple(Newsletter.model_fields)
_FEEDBACK_COLUMNS = ("newsletter_id", *ReviewerMessage.model_fields)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS newsletters (
    {", ".join(_NEWSLETTER_COLUMNS)},
    PRIMARY KEY (newsletter_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    {", ".join(_FEEDBACK_COLUMNS)},
    PRIMARY KEY (message_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newsletter_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    data BLOB NOT NULL
);
"""


class SqliteStore:
    """Each operation opens its own connection in a worker thread and runs one transaction."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def initialise(self) -> None:
        await asyncio.to_thread(self._create_files)
        await self._execute(lambda connection: connection.executescript(_SCHEMA))

    async def get_open_newsletter(self) -> Newsletter | None:
        placeholders = ", ".join("?" for _ in CLOSED_STATES)
        sql = f"SELECT * FROM newsletters WHERE state NOT IN ({placeholders})"
        row = await self._execute(lambda c: c.execute(sql, CLOSED_STATES).fetchone())
        return None if row is None else Newsletter.model_validate(dict(row))

    async def add_newsletter(self, newsletter: Newsletter) -> None:
        await self._insert("INSERT", "newsletters", newsletter.model_dump(mode="json"))

    async def record_feedback(self, newsletter_id: str, message: ReviewerMessage) -> None:
        row = {"newsletter_id": newsletter_id} | message.model_dump(mode="json")
        await self._insert("INSERT OR IGNORE", "feedback", row)

    async def load_history(self, newsletter_id: str) -> list[HistoryRow]:
        sql = "SELECT message_id, data FROM messages WHERE newsletter_id = ? ORDER BY id"
        rows = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchall())
        return [HistoryRow.model_validate(dict(row)) for row in rows]

    async def append_history(self, newsletter_id: str, message_id: str, data: bytes) -> None:
        row = {"newsletter_id": newsletter_id, "message_id": message_id, "data": data}
        await self._insert("INSERT", "messages", row)

    async def _insert(self, verb: str, table: str, row: Mapping[str, object]) -> None:
        columns = ", ".join(row)
        placeholders = ", ".join(f":{column}" for column in row)
        sql = f"{verb} INTO {table} ({columns}) VALUES ({placeholders})"
        await self._execute(lambda connection: connection.execute(sql, row))

    async def _execute[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._transaction, operation)

    def _transaction[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        try:
            with closing(sqlite3.connect(self._path)) as connection, connection:
                connection.row_factory = sqlite3.Row
                return operation(connection)
        except sqlite3.Error as error:
            raise StoreError(f"store operation failed: {type(error).__name__}") from error

    def _create_files(self) -> None:
        directory = self._path.parent
        if not directory.exists():
            directory.mkdir(parents=True)
            # mkdir applies the umask, so the mode is set afterwards.
            directory.chmod(0o700)
        os.close(os.open(self._path, os.O_CREAT | os.O_WRONLY, 0o600))
