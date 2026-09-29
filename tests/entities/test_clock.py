from datetime import UTC, datetime, timedelta

import pytest

from pulse.adapters.clock import SystemClock
from pulse.entities.clock import Clock
from tests.fakes.clock import ControlledClock


@pytest.fixture(params=["system", "controlled"])
def clock(request: pytest.FixtureRequest) -> Clock:
    if request.param == "system":
        return SystemClock()
    return ControlledClock(datetime(2026, 9, 29, 12, 0, tzinfo=UTC))


def test_now_is_aware_utc(clock: Clock) -> None:
    now = clock.now()

    assert now.utcoffset() == timedelta(0)


def test_controlled_clock_returns_the_set_time() -> None:
    clock = ControlledClock(datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    clock.time = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)

    assert clock.now() == datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
