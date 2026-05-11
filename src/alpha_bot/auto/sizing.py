"""Position sizing based on account risk parameters.

Computes the number of shares to buy for a given entry/stop pair, capped
by both risk-capital allocation and available cash.
"""

from __future__ import annotations

import logging

from alpha_bot.broker.base import Broker
from alpha_bot.models import Market

logger = logging.getLogger(__name__)


def compute_position_size(
    broker: Broker,
    market: Market,
    entry: float,
    stop: float,
    risk_pct: float,
) -> tuple[int, str]:
    """Return ``(quantity, note)``. quantity is 0 when sizing is impossible.

    Sizing rule: risk_capital = total_account_value × (risk_pct / 100); the
    per-share risk is ``entry - stop``. Quantity is also capped by available
    cash so we never queue an order we cannot fund.
    """

    if risk_pct <= 0 or entry <= 0 or stop <= 0:
        return 0, "risk_per_trade_pct/entry/stop 값이 유효하지 않음"
    per_share_risk = entry - stop
    if per_share_risk <= 0:
        return 0, f"진입가({entry:.2f}) ≤ 손절가({stop:.2f}), 사이징 불가"
    if not hasattr(broker, "get_cash_balance"):
        return 0, "브로커가 잔고 조회를 지원하지 않음"
    try:
        balance = broker.get_cash_balance(market)
    except Exception as exc:
        return 0, f"잔고 조회 실패: {exc}"
    risk_capital = balance.total_value * (risk_pct / 100.0)
    qty_from_risk = int(risk_capital // per_share_risk)
    # Cap by available cash. For US trading on KIS paper accounts the broker
    # may report cash=0 when the foreign-currency deposit table is unpopulated;
    # in that case we fall back to (total_value − securities_value) which the
    # KIS broker already estimates from output3.frcr_evlu_tota.
    available_cash = balance.cash
    if available_cash <= 0 and balance.total_value > balance.securities_value:
        available_cash = max(0.0, balance.total_value - balance.securities_value)
    qty_from_cash = int(available_cash // entry) if entry > 0 else 0
    qty = max(0, min(qty_from_risk, qty_from_cash))
    note = (
        f"총평가 {balance.total_value:.0f}{balance.currency} × {risk_pct}% "
        f"= {risk_capital:.0f}{balance.currency} 리스크자본 / "
        f"주당 {per_share_risk:.2f} → {qty_from_risk}주 "
        f"(가용현금 {available_cash:.0f}{balance.currency} 한도 {qty_from_cash}주)"
    )
    return qty, note
