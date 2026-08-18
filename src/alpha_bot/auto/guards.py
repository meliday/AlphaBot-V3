"""Trading guards — kill switch and daily-loss circuit breaker.

These are the "stop the bleeding" controls that sit above strategy logic:

  * Kill switch: a file at ``KILL_SWITCH`` (override via ``BOT_KILL_SWITCH``
    env var) blocks all NEW buys while it exists. Exit management for held
    positions keeps running — protecting open positions is exactly when a
    panicked operator needs the bot most. Delete the file to resume.

  * Daily loss limit: once today's realized losses (per market, computed
    from the approval queue's filled buy→sell pairs) reach
    ``daily_loss_limit_pct`` of account value, new buys in that market stop
    until the next calendar day. Catches both a broken strategy and a
    hostile tape before they can compound.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from alpha_bot.approval import ApprovalQueue
from alpha_bot.broker.base import Broker
from alpha_bot.models import Market, OrderCandidate

logger = logging.getLogger(__name__)

_DEFAULT_KILL_SWITCH = "KILL_SWITCH"


# ── Kill switch ──────────────────────────────────────────────────────


def kill_switch_path() -> Path:
    return Path(os.environ.get("BOT_KILL_SWITCH", _DEFAULT_KILL_SWITCH))


def kill_switch_active() -> str | None:
    """Return the reason string if the kill switch is engaged, else None.

    The file's first line (if any) is used as the operator-supplied reason.
    """
    path = kill_switch_path()
    if not path.exists():
        return None
    try:
        first_line = path.read_text(encoding="utf-8").strip().splitlines()
        reason = first_line[0] if first_line else ""
    except OSError:
        reason = ""
    return reason or "KILL_SWITCH 파일 존재"


# ── Daily loss circuit breaker ───────────────────────────────────────


def _fill_date(order: OrderCandidate) -> date | None:
    """Best-effort local calendar date of an order's fill/submission."""
    for stamp in (order.last_synced_at, order.submitted_at, order.created_at):
        if not stamp:
            continue
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().date()
    return None


def realized_pnl_today(queue: ApprovalQueue, market: Market) -> float:
    """Sum of realized P&L (market currency) from sells filled today.

    Pairs each filled sell with its parent buy via ``exit_order_id`` /
    ``partial_exit_ids`` and computes (sell_avg − buy_avg) × qty. Sells
    without a confirmed fill price (e.g. synthetic external-close rows)
    are skipped — we only count P&L the broker actually reported.
    """
    orders = queue.list_orders()
    sell_to_buy: dict[str, OrderCandidate] = {}
    for o in orders:
        if o.request.side != "buy":
            continue
        if o.exit_order_id:
            sell_to_buy[o.exit_order_id] = o
        for pid in o.partial_exit_ids:
            sell_to_buy[pid] = o

    today = date.today()
    pnl = 0.0
    for sell in orders:
        if sell.request.side != "sell" or sell.request.market != market:
            continue
        if sell.status not in {"filled", "partially_filled_cancelled"}:
            continue
        if sell.avg_fill_price is None or (sell.filled_quantity or 0) <= 0:
            continue
        buy = sell_to_buy.get(sell.id)
        if buy is None or buy.avg_fill_price is None:
            continue
        if _fill_date(sell) != today:
            continue
        pnl += (sell.avg_fill_price - buy.avg_fill_price) * sell.filled_quantity
    return pnl


def unpriced_external_closes_today(
    queue: ApprovalQueue, market: Market
) -> list[OrderCandidate]:
    """External closes whose execution price is unknown on the local day."""

    today = date.today()
    return [
        order
        for order in queue.list_orders()
        if order.request.side == "sell"
        and order.request.market == market
        and order.status in {"filled", "partially_filled_cancelled"}
        and order.broker_order_id == "EXTERNAL"
        and (order.filled_quantity or 0) > 0
        and order.avg_fill_price is None
        and _fill_date(order) == today
    ]


def daily_loss_exceeded(
    queue: ApprovalQueue,
    broker: Broker,
    market: Market,
    limit_pct: float,
) -> tuple[bool, str]:
    """Return ``(True, detail)`` when today's realized loss breaches the limit.

    Fail-open on balance-query errors: a monitoring failure must not freeze
    trading by itself (the hard stops still protect each position).
    """
    if limit_pct <= 0:
        return False, ""
    unpriced = unpriced_external_closes_today(queue, market)
    if unpriced:
        quantity = sum(order.filled_quantity for order in unpriced)
        return True, (
            f"당일 외부청산 {len(unpriced)}건({quantity}주)의 체결가가 없어 "
            "실현손익을 확인할 수 없음 — 신규 진입 안전 차단"
        )
    pnl = realized_pnl_today(queue, market)
    if pnl >= 0:
        return False, ""
    if not hasattr(broker, "get_cash_balance"):
        return False, ""
    try:
        balance = broker.get_cash_balance(market)
    except Exception as exc:
        logger.warning("Daily-loss check: balance query failed for %s: %s", market, exc)
        return False, ""
    total = balance.total_value
    if total <= 0:
        return False, ""
    loss_pct = -pnl / total * 100.0
    if loss_pct >= limit_pct:
        return True, (
            f"오늘 실현손실 {-pnl:,.0f}{balance.currency} "
            f"({loss_pct:.2f}% ≥ 한도 {limit_pct:.2f}%)"
        )
    return False, ""
