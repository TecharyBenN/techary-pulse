"""The SQLite store."""

import asyncio
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path

from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import StoreError
from pulse.entities.extracts import ExtractorOutput, ExtractRecord, make_record
from pulse.entities.lifecycle import CLOSED_STATES, Newsletter
from pulse.entities.store import HistoryRow
from pulse.entities.submissions import Submission

_NEWSLETTER_COLUMNS = tuple(Newsletter.model_fields)
_FEEDBACK_COLUMNS = ("newsletter_id", *ReviewerMessage.model_fields)
_SUBMISSION_COLUMNS = ("newsletter_id", *Submission.model_fields)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS newsletters (
    {", ".join(_NEWSLETTER_COLUMNS)},
    PRIMARY KEY (newsletter_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    {", ".join(_FEEDBACK_COLUMNS)},
    PRIMARY KEY (message_id)
);
CREATE TABLE IF NOT EXISTS submissions (
    {", ".join(_SUBMISSION_COLUMNS)},
    PRIMARY KEY (newsletter_id, message_id)
);
CREATE TABLE IF NOT EXISTS extract_records (
    newsletter_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    excluded_id TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY (newsletter_id, message_id)
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

    async def save_start(self, newsletter: Newsletter, submissions: Sequence[Submission]) -> None:
        newsletter_id = newsletter.newsletter_id
        rows = [{"newsletter_id": newsletter_id} | s.model_dump(mode="json") for s in submissions]

        def save(connection: sqlite3.Connection) -> None:
            _insert(
                connection, "INSERT OR REPLACE", "newsletters", newsletter.model_dump(mode="json")
            )
            for row in rows:
                _insert(connection, "INSERT OR IGNORE", "submissions", row)

        await self._execute(save)

    async def list_submissions(self, newsletter_id: str) -> list[Submission]:
        sql = "SELECT * FROM submissions WHERE newsletter_id = ?"
        rows = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchall())
        submissions = [Submission.model_validate(dict(row)) for row in rows]
        # Sorted here, because stored times may carry different UTC offsets.
        return sorted(submissions, key=lambda s: (s.received, s.message_id))

    async def save_extract(self, newsletter_id: str, output: ExtractorOutput) -> ExtractRecord:
        def save(connection: sqlite3.Connection) -> ExtractRecord:
            # Parallel extractions number their records, so the write lock is taken before reading.
            connection.execute("BEGIN IMMEDIATE")
            sql = (
                "SELECT message_id, excluded_id, data FROM extract_records WHERE newsletter_id = ?"
            )
            saved = connection.execute(sql, (newsletter_id,)).fetchall()
            previous = next(
                (_record(row) for row in saved if row["message_id"] == output.message_id), None
            )
            used_ids = [row["excluded_id"] for row in saved if row["excluded_id"]]
            record = make_record(output, previous, used_ids)
            row = {
                "newsletter_id": newsletter_id,
                "message_id": record.message_id,
                "excluded_id": record.excluded_id,
                "data": record.model_dump_json(),
            }
            # An upsert keeps the row's position, so records stay in the order first saved.
            connection.execute(
                "INSERT INTO extract_records (newsletter_id, message_id, excluded_id, data)"
                " VALUES (:newsletter_id, :message_id, :excluded_id, :data)"
                " ON CONFLICT (newsletter_id, message_id)"
                " DO UPDATE SET excluded_id = excluded.excluded_id, data = excluded.data",
                row,
            )
            return record

        return await self._execute(save)

    async def list_extract_records(self, newsletter_id: str) -> list[ExtractRecord]:
        sql = "SELECT data FROM extract_records WHERE newsletter_id = ? ORDER BY rowid"
        rows = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchall())
        return [_record(row) for row in rows]

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
        await self._execute(lambda connection: _insert(connection, verb, table, row))

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


def _insert(
    connection: sqlite3.Connection, verb: str, table: str, row: Mapping[str, object]
) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join(f":{column}" for column in row)
    connection.execute(f"{verb} INTO {table} ({columns}) VALUES ({placeholders})", row)


def _record(row: sqlite3.Row) -> ExtractRecord:
    return ExtractRecord.model_validate_json(row["data"])
