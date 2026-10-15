from pathlib import Path

import pytest

from pulse.adapters.store import SqliteStore


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "pulse.db"


@pytest.fixture
async def store(db_path: Path) -> SqliteStore:
    """An empty store in a temporary folder."""
    store = SqliteStore(db_path)
    await store.initialise()
    return store
