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
