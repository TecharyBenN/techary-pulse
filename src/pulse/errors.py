"""Error types raised by Pulse."""


class PulseError(Exception):
    """Base class for errors raised by Pulse."""


class ConfigError(PulseError):
    """Configuration is missing or invalid. Pulse stops at start-up."""


class AgentResponseError(PulseError):
    """An agent returned an invalid response twice. Pulse is faulty; the run fails."""

    def __init__(self, agent: str, subject: str, detail: str) -> None:
        super().__init__(f"{agent} returned an invalid response for {subject}: {detail}")
        self.agent = agent
        self.subject = subject
        self.detail = detail


class GatewayError(PulseError):
    """The AI gateway timed out, throttled the request or returned an error."""


class LockHeldError(PulseError):
    """Another run holds the lock."""


class StopRequested(PulseError):
    """SIGTERM was received; the run stops at the next step boundary."""


class GraphError(PulseError):
    """Microsoft Graph or the Entra ID token endpoint returned an error."""


class EditionStoreError(PulseError):
    """The edition store rejected a write, such as a duplicate item ID."""


class EditionRefused(PulseError):
    """An edition transition was refused because its preconditions were not met."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConversationError(PulseError):
    """A chat agent run failed: the gateway or a tool raised. Nothing is appended to history."""
