"""Pulse's exception types."""


class PulseError(Exception):
    """Base class for every error Pulse raises."""


class Refusal(PulseError):
    """A rule refused an action; the message is the reason a tool returns."""


class StoreError(PulseError):
    """The store could not be read or written."""


class RunFailed(PulseError):
    """An orchestrator run did not complete; the steps it completed are saved."""


class InvalidToken(PulseError):
    """A bearer token failed verification."""


class MailboxError(PulseError):
    """A mailbox operation failed; the message names the status, never the content."""


class SpecialistFailed(PulseError):
    """A specialist agent gave no valid response after its retry."""
