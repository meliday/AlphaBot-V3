"""Position sizing based on account risk parameters.

Computes the number of shares to buy for a given entry/stop pair, capped
by both risk-capital allocation and available cash.
"""

from __future__ import annotations

import logging

from alpha_bot.broker.base import Broker
from alpha_bot.models import AccountBalance, Market

logger = logging.getLogger(__name__)


def usable_cash(balance: AccountBalance) -> float:
    """Cash actually available for a buy.

    KIS paper accounts sometimes report cash=0 for US trading when the
    foreign-currency deposit table is unpopulated; fall back to
    (total_value − securities_value), which the KIS broker estimates from
    output3.frcr_evlu_tota. Shared by auto-sizing and the orchestrator's
    pre-flight so the two can't disagree about affordability.
    """
    cash = balance.cash
    if cash <= 0 and balance.total_value > balance.securities_value:
        cash = max(0.0, balance.total_value - balance.securities_value)
    return cash


def compute_position_size(
    broker: Broker,
    market: Market,
    entry: float,
    stop: float,
    risk_pct: float,
    max_position_pct: float = 0.0,
) -> tuple[int, str]:
    """Return ``(quantity, note)``. quantity is 0 when sizing is impossible.

    Sizing rule: risk_capital = total_account_value × (risk_pct / 100); the
    per-share risk is ``entry - stop``. Quantity is further capped by:
      * available cash — never queue an order we cannot fund;
      * ``max_position_pct`` of account value — a tight stop must not
        concentrate the account into one name (2.5% stop × 1% risk would
        otherwise size to ~40% of equity). 0 disables the cap.
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
    available_cash = usable_cash(balance)
    qty_from_cash = int(available_cash // entry) if entry > 0 else 0
    qty = max(0, min(qty_from_risk, qty_from_cash))
    cap_note = ""
    if max_position_pct > 0:
        position_budget = balance.total_value * (max_position_pct / 100.0)
        qty_from_cap = int(position_budget // entry)
        if qty_from_cap < qty:
            cap_note = f", 포지션 상한 {max_position_pct:.0f}% → {qty_from_cap}주로 축소"
        qty = max(0, min(qty, qty_from_cap))
    note = (
        f"총평가 {balance.total_value:.0f}{balance.currency} × {risk_pct}% "
        f"= {risk_capital:.0f}{balance.currency} 리스크자본 / "
        f"주당 {per_share_risk:.2f} → {qty_from_risk}주 "
        f"(가용현금 {available_cash:.0f}{balance.currency} 한도 {qty_from_cash}주{cap_note})"
    )
    return qty, note
