"""Mock broker simulation web API handlers."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def handle_mock_state() -> dict[str, Any]:
    from alpha_bot.broker.mock import MockBroker
    broker = MockBroker()
    out: dict[str, Any] = {"markets": {}, "ledger": []}
    for market in ("KR", "US"):
        try:
            bal = broker.get_cash_balance(market)  # type: ignore[arg-type]
            pos = broker.get_positions(market)  # type: ignore[arg-type]
            out["markets"][market] = {
                "starting_cash": broker.get_starting_cash(market),  # type: ignore[arg-type]
                "cash": bal.cash,
                "securities_value": bal.securities_value,
                "total_value": bal.total_value,
                "currency": bal.currency,
                "positions": [
                    {"ticker": p.ticker, "quantity": p.quantity,
                     "avg_price": p.avg_price, "market_value": p.market_value}
                    for p in pos
                ],
            }
        except Exception as exc:
            logger.warning("Mock state for %s failed: %s", market, exc)
            out["markets"][market] = {"error": str(exc)}
    out["ledger"] = list(reversed(broker.list_orders_raw()))
    return out


def handle_mock_order(body: dict[str, Any]) -> dict[str, Any] | tuple[str, int]:
    from alpha_bot.broker.mock import MockBroker
    ticker = str(body.get("ticker", "")).strip().upper()
    market = str(body.get("market", "")).upper()
    side = str(body.get("side", "")).lower()
    try:
        quantity = int(body.get("quantity", 0))
        price = float(body.get("price", 0))
    except (TypeError, ValueError):
        return ("quantity/price must be numeric", 400)
    if not ticker or market not in ("KR", "US"):
        return ("ticker/market required (market in KR|US)", 400)
    if side not in ("buy", "sell"):
        return ("side must be 'buy' or 'sell'", 400)
    if quantity <= 0 or price <= 0:
        return ("quantity and price must be positive", 400)
    broker = MockBroker()
    try:
        result = broker.place_manual_order(ticker, market, side, quantity, price)
    except Exception as exc:
        return (str(exc), 500)
    return {"broker_order_id": result.broker_order_id, "accepted": result.accepted, "message": result.message}


def handle_mock_set_cash(body: dict[str, Any]) -> dict[str, Any] | tuple[str, int]:
    from alpha_bot.broker.mock import MockBroker
    market = str(body.get("market", "")).upper()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return ("amount must be numeric", 400)
    if market not in ("KR", "US"):
        return ("market must be KR or US", 400)
    if amount < 0:
        return ("amount must be non-negative", 400)
    MockBroker().set_starting_cash(market, amount)  # type: ignore[arg-type]
    return {"market": market, "starting_cash": amount}


def handle_mock_inject(body: dict[str, Any]) -> dict[str, Any] | tuple[str, int]:
    from alpha_bot.broker.mock import MockBroker
    ticker = str(body.get("ticker", "")).strip().upper()
    market = str(body.get("market", "")).upper()
    try:
        quantity = int(body.get("quantity", 0))
        avg_price = float(body.get("avg_price", 0))
    except (TypeError, ValueError):
        return ("quantity/avg_price must be numeric", 400)
    if not ticker or market not in ("KR", "US"):
        return ("ticker/market required", 400)
    if quantity <= 0 or avg_price <= 0:
        return ("quantity and avg_price must be positive", 400)
    try:
        result = MockBroker().inject_position(ticker, market, quantity, avg_price)  # type: ignore[arg-type]
    except Exception as exc:
        return (str(exc), 500)
    return {"broker_order_id": result.broker_order_id, "message": result.message}


def handle_mock_reset(body: dict[str, Any]) -> dict[str, Any]:
    from alpha_bot.broker.mock import MockBroker
    keep_cash = bool(body.get("keep_starting_cash", True))
    MockBroker().reset_state(keep_starting_cash=keep_cash)
    return {"reset": True, "kept_starting_cash": keep_cash}


def handle_mock_delete_order(body: dict[str, Any]) -> dict[str, Any] | tuple[str, int]:
    from alpha_bot.broker.mock import MockBroker
    order_id = str(body.get("broker_order_id", "")).strip()
    if not order_id:
        return ("broker_order_id required", 400)
    removed = MockBroker().delete_order(order_id)
    if not removed:
        return ("order not found", 404)
    return {"removed": order_id}
