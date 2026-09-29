"""Pulse's exception types."""


class PulseError(Exception):
    """Base class for every error Pulse raises."""


class Refusal(PulseError):
    """A rule refused an action; the message is the reason a tool returns."""
