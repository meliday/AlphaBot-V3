from __future__ import annotations

from typing import Protocol

from alpha_bot.models import (
    AccountBalance,
    Market,
    OrderFill,
    OrderRequest,
    OrderResult,
    Position,
)


class Broker(Protocol):
    name: str

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
