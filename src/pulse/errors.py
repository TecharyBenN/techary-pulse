"""Error types raised by Pulse."""


class PulseError(Exception):
    """Base class for errors raised by Pulse."""


class ConfigError(PulseError):
    """Configuration is missing or invalid. Pulse stops at start-up."""
