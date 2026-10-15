import asyncio
import stat
import sys
from pathlib import Path

import pytest

from pulse.adapters.lock import RunLock

pytestmark = pytest.mark.anyio


async def _taken(lock: RunLock) -> bool:
    """Whether the lock can be taken within a short wait, releasing it again if so."""
    try:
        async with asyncio.timeout(0.3):
            await lock.__aenter__()
    except TimeoutError:
        return False
    await lock.__aexit__(None, None, None)
    return True


async def test_locks_on_one_file_exclude_each_other(tmp_path: Path) -> None:
    # Each lock stands for one Pulse process, such as pulse serve and pulse draft.
    path = tmp_path / "pulse.lock"
    serve, draft = RunLock(path), RunLock(path)

    async with serve:
        assert not await _taken(draft)

    assert await _taken(draft)


# Another Pulse process, such as pulse draft: it holds the lock until its input closes.
_HOLDER = """
import asyncio, sys
from pathlib import Path
from pulse.adapters.lock import RunLock

async def hold():
    async with RunLock(Path(sys.argv[1])):
        print("locked", flush=True)
        await asyncio.to_thread(sys.stdin.read)

asyncio.run(hold())
"""


async def test_a_lock_held_by_another_process_is_waited_for(tmp_path: Path) -> None:
    path = tmp_path / "pulse.lock"
    holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _HOLDER,
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert holder.stdin is not None and holder.stdout is not None
    assert await asyncio.wait_for(holder.stdout.readline(), timeout=10) == b"locked\n"

    assert not await _taken(RunLock(path))

    holder.stdin.close()
    await asyncio.wait_for(holder.wait(), timeout=10)
    assert await _taken(RunLock(path))


async def test_a_cancelled_wait_leaves_the_lock_free(tmp_path: Path) -> None:
    path = tmp_path / "pulse.lock"
    serve, draft = RunLock(path), RunLock(path)

    async with serve:
        waiting = asyncio.create_task(draft.__aenter__())
        await asyncio.sleep(0.2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    assert await _taken(draft)
    assert await _taken(serve)


async def test_waiters_in_one_process_take_the_lock_in_arrival_order(tmp_path: Path) -> None:
    lock = RunLock(tmp_path / "pulse.lock")
    order: list[int] = []

    async def run(n: int) -> None:
        async with lock:
            order.append(n)
            await asyncio.sleep(0.01)

    await asyncio.gather(*(run(n) for n in range(5)))

    assert order == list(range(5))


async def test_the_lock_file_is_readable_only_by_pulse(tmp_path: Path) -> None:
    path = tmp_path / "pulse.lock"

    async with RunLock(path):
        pass

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
