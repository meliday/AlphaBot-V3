class BotError(Exception):
    """Base exception for recoverable bot failures."""


class DataError(BotError):
    """Raised when market, fundamental, or catalyst data is missing or invalid."""


class StrategyError(BotError):
    """Raised when strategy evaluation cannot be completed."""


class BrokerError(BotError):
    """Raised when broker configuration or order submission fails."""


class BrokerOrderRejected(BrokerError):
    """The broker definitively rejected an order before it could execute."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ApprovalError(BotError):
    """Raised when an order cannot be queued or approved safely."""
