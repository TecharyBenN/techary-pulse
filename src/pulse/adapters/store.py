"""The SQLite store: SqliteStore implements Store."""

import asyncio
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path

from pydantic import TypeAdapter

from pulse.entities.content import Verdict, Version, WriterOutput
from pulse.entities.conversation import HandledMessage, ReviewerMessage
from pulse.entities.errors import StoreError
from pulse.entities.extracts import Consolidation, Extraction, ExtractRecord, make_record
from pulse.entities.lifecycle import CLOSED_STATES, Newsletter
from pulse.entities.mail import MessageId, ScreenedEmail
from pulse.entities.store import HistoryRow

_NEWSLETTER_COLUMNS = tuple(Newsletter.model_fields)
_FEEDBACK_COLUMNS = ("newsletter_id", *ReviewerMessage.model_fields)
_SCREENED_EMAIL_COLUMNS = ("newsletter_id", *ScreenedEmail.model_fields)
_VERDICTS = TypeAdapter(list[Verdict])

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS newsletters (
    {", ".join(_NEWSLETTER_COLUMNS)},
    PRIMARY KEY (newsletter_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    {", ".join(_FEEDBACK_COLUMNS)},
    PRIMARY KEY (newsletter_id, message_id)
);
CREATE TABLE IF NOT EXISTS screened_emails (
    {", ".join(_SCREENED_EMAIL_COLUMNS)},
    PRIMARY KEY (newsletter_id, message_id)
);
CREATE TABLE IF NOT EXISTS extract_records (
    newsletter_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    excluded_id TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY (newsletter_id, message_id)
);
CREATE TABLE IF NOT EXISTS items (
    newsletter_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (newsletter_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    newsletter_id TEXT NOT NULL,
    data TEXT NOT NULL,
    verdicts TEXT,
    PRIMARY KEY (newsletter_id)
);
CREATE TABLE IF NOT EXISTS versions (
    newsletter_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (newsletter_id, version)
);
CREATE TABLE IF NOT EXISTS handled_messages (
    message_id TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    handled INTEGER NOT NULL,
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
    """Pulse's state in one SQLite file; each operation runs in its own transaction."""

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

    async def get_latest_newsletter(self) -> Newsletter | None:
        rows = await self._execute(lambda c: c.execute("SELECT * FROM newsletters").fetchall())
        newsletters = [Newsletter.model_validate(dict(row)) for row in rows]
        # Compared here, because stored times may carry different UTC offsets.
        return max(newsletters, key=lambda n: n.opened_at, default=None)

    async def save_newsletter(self, newsletter: Newsletter) -> None:
        await self._execute(lambda connection: _save_newsletter(connection, newsletter))

    async def save_start(self, newsletter: Newsletter, emails: Sequence[ScreenedEmail]) -> None:
        newsletter_id = newsletter.newsletter_id
        rows = [{"newsletter_id": newsletter_id} | e.model_dump(mode="json") for e in emails]

        def save(connection: sqlite3.Connection) -> None:
            _save_newsletter(connection, newsletter)
            for row in rows:
                _insert(connection, "INSERT OR IGNORE", "screened_emails", row)

        await self._execute(save)

    async def list_screened_emails(self, newsletter_id: str) -> list[ScreenedEmail]:
        sql = "SELECT * FROM screened_emails WHERE newsletter_id = ?"
        rows = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchall())
        emails = [ScreenedEmail.model_validate(dict(row)) for row in rows]
        # Sorted here, because stored times may carry different UTC offsets.
        return sorted(emails, key=lambda e: (e.received, e.message_id))

    async def mark_moved(self, newsletter_id: str, message_id: MessageId) -> None:
        sql = "UPDATE screened_emails SET moved = 1 WHERE newsletter_id = ? AND message_id = ?"
        await self._execute(lambda connection: connection.execute(sql, (newsletter_id, message_id)))

    async def save_extract(
        self, newsletter_id: str, message_id: MessageId, extraction: Extraction
    ) -> ExtractRecord:
        def save(connection: sqlite3.Connection) -> ExtractRecord:
            # Parallel extractions number their records, so the write lock is taken before reading.
            connection.execute("BEGIN IMMEDIATE")
            sql = (
                "SELECT message_id, excluded_id, data FROM extract_records WHERE newsletter_id = ?"
            )
            saved = connection.execute(sql, (newsletter_id,)).fetchall()
            # Each email is extracted once, so an extraction that ran at the same time as
            # another of the same email changes nothing.
            for row in saved:
                if row["message_id"] == message_id:
                    return _record(row)
            used_ids = [row["excluded_id"] for row in saved if row["excluded_id"]]
            record = make_record(message_id, extraction, used_ids)
            row = {
                "newsletter_id": newsletter_id,
                "message_id": record.message_id,
                "excluded_id": record.excluded_id,
                "data": record.model_dump_json(),
            }
            _insert(connection, "INSERT", "extract_records", row)
            return record

        return await self._execute(save)

    async def save_restored(self, newsletter_id: str, record: ExtractRecord) -> None:
        sql = "UPDATE extract_records SET data = ? WHERE newsletter_id = ? AND message_id = ?"
        params = (record.model_dump_json(), newsletter_id, record.message_id)
        await self._execute(lambda connection: connection.execute(sql, params))

    async def list_extract_records(self, newsletter_id: str) -> list[ExtractRecord]:
        sql = "SELECT data FROM extract_records WHERE newsletter_id = ? ORDER BY rowid"
        rows = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchall())
        return [_record(row) for row in rows]

    async def save_items(self, newsletter_id: str, consolidation: Consolidation) -> None:
        row = {"newsletter_id": newsletter_id, "data": consolidation.model_dump_json()}
        await self._insert("INSERT OR REPLACE", "items", row)

    async def get_items(self, newsletter_id: str) -> Consolidation | None:
        sql = "SELECT data FROM items WHERE newsletter_id = ?"
        row = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchone())
        return None if row is None else Consolidation.model_validate_json(row["data"])

    async def save_draft(self, newsletter_id: str, draft: WriterOutput) -> None:
        row = {"newsletter_id": newsletter_id, "data": draft.model_dump_json()}
        await self._insert("INSERT OR REPLACE", "drafts", row)

    async def get_draft(self, newsletter_id: str) -> WriterOutput | None:
        sql = "SELECT data FROM drafts WHERE newsletter_id = ?"
        row = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchone())
        return None if row is None else WriterOutput.model_validate_json(row["data"])

    async def save_verdicts(self, newsletter_id: str, verdicts: Sequence[Verdict]) -> None:
        sql = "UPDATE drafts SET verdicts = ? WHERE newsletter_id = ?"
        params = (_VERDICTS.dump_json(list(verdicts)).decode(), newsletter_id)
        await self._execute(lambda connection: connection.execute(sql, params))

    async def get_verdicts(self, newsletter_id: str) -> list[Verdict] | None:
        sql = "SELECT verdicts FROM drafts WHERE newsletter_id = ?"
        row = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchone())
        if row is None or row["verdicts"] is None:
            return None
        return _VERDICTS.validate_json(row["verdicts"])

    async def save_version(self, newsletter: Newsletter, version: Version) -> None:
        row = {
            "newsletter_id": newsletter.newsletter_id,
            "version": version.version,
            "data": version.model_dump_json(),
        }

        def save(connection: sqlite3.Connection) -> None:
            _save_newsletter(connection, newsletter)
            # A plain insert, so a version number is never reused.
            _insert(connection, "INSERT", "versions", row)

        await self._execute(save)

    async def get_version(self, newsletter_id: str, version: int) -> Version | None:
        sql = "SELECT data FROM versions WHERE newsletter_id = ? AND version = ?"
        row = await self._execute(lambda c: c.execute(sql, (newsletter_id, version)).fetchone())
        return None if row is None else Version.model_validate_json(row["data"])

    async def record_feedback(self, newsletter_id: str, message: ReviewerMessage) -> None:
        row = {"newsletter_id": newsletter_id} | message.model_dump(mode="json")
        await self._insert("INSERT OR IGNORE", "feedback", row)

    async def list_feedback(self, newsletter_id: str) -> list[ReviewerMessage]:
        sql = "SELECT * FROM feedback WHERE newsletter_id = ? ORDER BY rowid"
        rows = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchall())
        return [ReviewerMessage.model_validate(dict(row)) for row in rows]

    async def load_history(self, newsletter_id: str) -> list[HistoryRow]:
        sql = "SELECT message_id, data FROM messages WHERE newsletter_id = ? ORDER BY id"
        rows = await self._execute(lambda c: c.execute(sql, (newsletter_id,)).fetchall())
        return [HistoryRow.model_validate(dict(row)) for row in rows]

    async def append_history(self, newsletter_id: str, message_id: str, data: bytes) -> None:
        row = {"newsletter_id": newsletter_id, "message_id": message_id, "data": data}
        await self._insert("INSERT", "messages", row)

    async def record_attempt(self, message_id: str) -> int:
        sql = (
            "INSERT INTO handled_messages (message_id, attempts, handled) VALUES (?, 1, 0)"
            " ON CONFLICT (message_id) DO UPDATE SET attempts = attempts + 1"
            " RETURNING attempts"
        )
        row = await self._execute(lambda c: c.execute(sql, (message_id,)).fetchone())
        attempts: int = row["attempts"]
        return attempts

    async def mark_handled(self, message_id: str) -> None:
        sql = "UPDATE handled_messages SET handled = 1 WHERE message_id = ?"
        await self._execute(lambda connection: connection.execute(sql, (message_id,)))

    async def get_handled_message(self, message_id: str) -> HandledMessage | None:
        sql = "SELECT * FROM handled_messages WHERE message_id = ?"
        row = await self._execute(lambda c: c.execute(sql, (message_id,)).fetchone())
        return None if row is None else HandledMessage.model_validate(dict(row))

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


def _save_newsletter(connection: sqlite3.Connection, newsletter: Newsletter) -> None:
    _insert(connection, "INSERT OR REPLACE", "newsletters", newsletter.model_dump(mode="json"))


def _record(row: sqlite3.Row) -> ExtractRecord:
    return ExtractRecord.model_validate_json(row["data"])
