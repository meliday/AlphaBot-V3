from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation

from alpha_bot.errors import BrokerOrderRejected
from alpha_bot.models import Market, Side


_KR_ETP_TYPES = {"ETF", "FOREIGN_ETF", "ETN"}
_KR_WARRANT_TYPES = {"STOCK_WARRANTS", "ELW"}


def tick_size(
    price: float | Decimal,
    market: Market,
    *,
    security_type: str | None = None,
) -> Decimal:
    value = _price_decimal(price)
    if market == "US":
        return Decimal("0.0001") if value < 1 else Decimal("0.01")

    kind = (security_type or "").upper()
    if kind in _KR_WARRANT_TYPES:
        return Decimal("5")
    if kind in _KR_ETP_TYPES:
        return Decimal("1") if value < 2_000 else Decimal("5")
    if value < 2_000:
        return Decimal("1")
    if value < 5_000:
        return Decimal("5")
    if value < 20_000:
        return Decimal("10")
    if value < 50_000:
        return Decimal("50")
    if value < 200_000:
        return Decimal("100")
    if value < 500_000:
        return Decimal("500")
    return Decimal("1000")


def round_order_price(
    price: float | Decimal,
    market: Market,
    side: Side,
    *,
    security_type: str | None = None,
) -> float:
    """Round passively: buys down, sells up, to a valid exchange tick."""

    value = _price_decimal(price)
    tick = tick_size(value, market, security_type=security_type)
    rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
    ticks = (value / tick).to_integral_value(rounding=rounding)
    return float(ticks * tick)


def round_aggressive_order_price(
    price: float | Decimal,
    market: Market,
    side: Side,
    *,
    security_type: str | None = None,
) -> float:
    """Round toward faster execution: buys up and sells down."""

    value = _price_decimal(price)
    tick = tick_size(value, market, security_type=security_type)
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    ticks = (value / tick).to_integral_value(rounding=rounding)
    return float(ticks * tick)


def floor_to_tick(
    price: float | Decimal,
    market: Market,
    *,
    security_type: str | None = None,
) -> float:
    """Aggressive sell/stop price that never rounds above the input."""

    value = _price_decimal(price)
    tick = tick_size(value, market, security_type=security_type)
    return float((value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)


def _price_decimal(value: float | Decimal) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BrokerOrderRejected(f"Invalid order price: {value}") from exc
    if not price.is_finite() or price <= 0:
        raise BrokerOrderRejected(f"Order price must be positive: {value}")
    return price
