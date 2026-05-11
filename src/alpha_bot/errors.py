class BotError(Exception):
    """Base exception for recoverable bot failures."""


class DataError(BotError):
    """Raised when market, fundamental, or catalyst data is missing or invalid."""


class StrategyError(BotError):
    """Raised when strategy evaluation cannot be completed."""


class BrokerError(BotError):
    """Raised when broker configuration or order submission fails."""


class ApprovalError(BotError):
    """Raised when an order cannot be queued or approved safely."""
