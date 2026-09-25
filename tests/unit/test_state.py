import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pulse.errors import LockHeldError
from pulse.state import Manifest, Outcome, RunArtefacts, delete_old_runs, latest_manifest, run_lock


def at(day: int) -> datetime:
    return datetime(2026, 9, day, 17, 30, tzinfo=UTC)


def test_second_lock_fails_while_first_is_held(tmp_path: Path) -> None:
    lock = tmp_path / "pulse.lock"
    with run_lock(lock), pytest.raises(LockHeldError), run_lock(lock):
        pass
    with run_lock(lock):
        pass


def test_artefacts_are_readable_only_by_owner(tmp_path: Path) -> None:
    artefacts = RunArtefacts(tmp_path / "runs", at(25))
    artefacts.write_text("x.txt", "x")
    assert stat.S_IMODE(artefacts.dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((artefacts.dir / "x.txt").stat().st_mode) == 0o600
    assert artefacts.dir.name == "20260925T173000Z"


def save(root: Path, day: int, dry_run: bool) -> None:
    RunArtefacts(root, at(day)).save(Manifest(run_id=str(day), started_at=at(day), dry_run=dry_run))


def test_latest_manifest_ignores_dry_runs(tmp_path: Path) -> None:
    save(tmp_path, 18, dry_run=False)
    save(tmp_path, 25, dry_run=True)
    found = latest_manifest(tmp_path)
    assert found is not None and found[1].run_id == "18"


def test_retention_deletes_only_old_runs(tmp_path: Path) -> None:
    save(tmp_path, 1, dry_run=False)
    save(tmp_path, 25, dry_run=False)
    (tmp_path / "pulse.lock").touch()
    deleted = delete_old_runs(tmp_path, at(25), retention_days=14)
    assert [d.name for d in deleted] == ["20260901T173000Z"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["20260925T173000Z", "pulse.lock"]


def test_pending_moves_are_classified_but_unmoved() -> None:
    manifest = Manifest(run_id="r", started_at=at(25), dry_run=False, message_ids=["a", "b", "c"])
    manifest.outcomes = {i: Outcome(status="included") for i in ("a", "b")}
    manifest.moved = ["a"]
    assert manifest.pending_moves() == ["b"]
