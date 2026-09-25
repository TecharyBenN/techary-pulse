"""Run state: the lock, the run manifest and artefacts, and retention."""

import fcntl
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from pulse.errors import LockHeldError


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the run.

    Raises:
        LockHeldError: If another run holds the lock.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockHeldError(f"another run holds {path}") from exc
        yield
    finally:
        os.close(fd)


RUN_DIR_FORMAT = "%Y%m%dT%H%M%SZ"
MANIFEST = "manifest.json"


class Outcome(BaseModel):
    status: Literal["rejected", "excluded", "included"]
    reason: str | None = None


class Manifest(BaseModel):
    """What a run has done so far, so the next run can recover."""

    run_id: str
    started_at: datetime
    dry_run: bool
    step: str = "started"
    message_ids: list[str] = []
    outcomes: dict[str, Outcome] = {}
    sent: bool = False
    moved: list[str] = []
    complete: bool = False

    def pending_moves(self) -> list[str]:
        return [i for i in self.message_ids if i in self.outcomes and i not in self.moved]


def _write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)


class RunArtefacts:
    """One directory per run, readable only by the account Pulse runs as."""

    def __init__(self, root: Path, started_at: datetime) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.dir = root / started_at.strftime(RUN_DIR_FORMAT)
        self.dir.mkdir(mode=0o700)

    def write_text(self, name: str, text: str) -> None:
        _write(self.dir / name, text)

    def write_json(self, name: str, data: BaseModel) -> None:
        _write(self.dir / name, data.model_dump_json(indent=2))

    def save(self, manifest: Manifest) -> None:
        save_manifest(self.dir, manifest)


def save_manifest(run_dir: Path, manifest: Manifest) -> None:
    """Rewrite the manifest of an earlier run, used when completing its moves."""
    _write(run_dir / MANIFEST, manifest.model_dump_json(indent=2))


def latest_manifest(root: Path) -> tuple[Path, Manifest] | None:
    """Return the most recent run's directory and manifest, ignoring dry runs."""
    if not root.exists():
        return None
    for run_dir in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True):
        path = run_dir / MANIFEST
        if path.exists():
            manifest = Manifest.model_validate_json(path.read_text(encoding="utf-8"))
            if not manifest.dry_run:
                return run_dir, manifest
    return None


def delete_old_runs(root: Path, now: datetime, retention_days: int) -> list[Path]:
    """Delete run directories older than ``retention_days`` and return them."""
    if not root.exists():
        return []
    cutoff = now - timedelta(days=retention_days)
    deleted = []
    for run_dir in root.iterdir():
        try:
            started = datetime.strptime(run_dir.name, RUN_DIR_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            continue
        if run_dir.is_dir() and started < cutoff:
            shutil.rmtree(run_dir)
            deleted.append(run_dir)
    return deleted
