from datetime import datetime


class ControlledClock:
    """A clock that returns whatever time the test sets."""

    def __init__(self, time: datetime) -> None:
        self.time = time

    def __call__(self) -> datetime:
        return self.time
