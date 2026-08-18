from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from alpha_bot.models import (
    AccountBalance,
    Market,
    OrderFill,
    OrderRequest,
    OrderResult,
    Position,
)


@dataclass(frozen=True)
class BrokerScope:
    """Stable identity of one concrete broker account/session.

    ``name`` alone is not sufficient: KIS paper/live accounts and, later,
    multiple Toss accounts can all use the same adapter name.  Orders are
    bound to this scope before the network call so another broker instance
    can never sync, cancel, or liquidate them accidentally.
    """

    name: str
    instance_id: str
    account_id: str
    mode: str


def broker_scope(broker: object) -> BrokerScope:
    """Return a scope for a broker, with safe defaults for test adapters."""

    name = str(getattr(broker, "name", broker.__class__.__name__.lower()))
    instance_id = str(getattr(broker, "instance_id", "") or f"{name}:default")
    account_id = str(getattr(broker, "account_id", "") or instance_id)
    mode = str(getattr(broker, "mode", "") or "default")
    return BrokerScope(name, instance_id, account_id, mode)


class Broker(Protocol):
    name: str

    @property
    def instance_id(self) -> str:
        ...

    @property
    def account_id(self) -> str:
        ...

    @property
    def mode(self) -> str:
        ...

    def place_order(self, order: OrderRequest) -> OrderResult:
        ...

    def get_cash_balance(self, market: Market) -> AccountBalance:
        ...

    def get_positions(self, market: Market) -> list[Position]:
        ...

    def get_order_fill(
        self, broker_order_id: str, market: Market, ordered_quantity: int
    ) -> OrderFill:
        ...

    def cancel_order(
        self, broker_order_id: str, market: Market, ticker: str, quantity: int
    ) -> OrderResult:
        """Cancel the unfilled remainder of a working order."""
        ...


class ProtectiveStopBroker(Protocol):
    """Optional capability: broker-side stop orders that survive bot downtime.

    Implemented by adapters whose venue can watch a price and submit the
    exit itself (Toss conditional orders). Detect support with
    :func:`supports_protective_stops` rather than ``isinstance`` — the rest
    of the codebase duck-types broker capabilities the same way.

    Implementations must be idempotent per ``client_order_id`` where the
    venue allows it, and must raise ``BrokerOrderRejected`` for permanent
    failures so the caller can stop retrying.
    """

    def place_protective_stop(
        self,
        *,
        ticker: str,
        market: Market,
        quantity: int,
        stop_price: float,
        client_order_id: str,
    ) -> str:
        """Arm a stop that sells ``quantity`` at market once price <= stop. Returns its id."""
        ...

    def cancel_protective_stop(self, stop_id: str) -> None:
        ...

    def protective_stop_status(self, stop_id: str) -> str | None:
        """Venue status, or None when the stop no longer exists."""
        ...


# No "amend" in the capability on purpose: Toss's modify endpoint has no
# idempotency key, so a lost response strands an untracked stop. Re-arming is
# always cancel + idempotent create, orchestrated by auto/protective_stops.
_PROTECTIVE_STOP_METHODS = (
    "place_protective_stop",
    "cancel_protective_stop",
    "protective_stop_status",
)


def supports_protective_stops(broker: object) -> bool:
    return all(callable(getattr(broker, name, None)) for name in _PROTECTIVE_STOP_METHODS)


# Venue statuses that mean the stop has fired and an exit order now exists or
# is being created. While a stop is in one of these states the polling ladder
# must not submit its own sell for the same shares.
PROTECTIVE_STOP_ENGAGED_STATUSES = frozenset({"ORDERING", "ORDERED"})
# Statuses meaning the stop is gone and the position needs a fresh one.
PROTECTIVE_STOP_DEAD_STATUSES = frozenset({"COMPLETED", "EXPIRED", "CANCELED"})


class TradabilityBroker(Protocol):
    """Optional capability: venue-side "may I buy this right now?" checks.

    Covers state a price/volume screen cannot see — delisting procedures,
    trading suspensions, exchange designations, momentary volatility halts.
    Implementations raise on transport failure rather than returning None,
    so callers can distinguish "verified tradable" from "unknown" and fail
    closed on the latter.
    """

    def tradability_block(self, ticker: str, market: Market) -> str | None:
        """Human-readable reason the symbol is unbuyable, or None if clear."""
        ...


def supports_tradability_checks(broker: object) -> bool:
    return callable(getattr(broker, "tradability_block", None))


class PortfolioValuationBroker(Protocol):
    """Optional capability: whole-account value in one currency.

    ``get_cash_balance`` reports a single market's sleeve, which is what a
    cash pre-flight needs. Risk sizing needs the opposite — a percentage of
    the *portfolio* — and summing sleeves requires an FX conversion only the
    venue can price. Implementations raise on failure so callers can fall
    back to sleeve-based sizing explicitly.
    """

    def portfolio_value(self, currency: str) -> float:
        ...


def supports_portfolio_valuation(broker: object) -> bool:
    return callable(getattr(broker, "portfolio_value", None))
