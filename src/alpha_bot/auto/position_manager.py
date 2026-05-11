"""Position lifecycle management — stop/target monitoring, reconciliation, force-exit.

Handles the post-fill side of the order lifecycle:
  * Check held positions against stop-loss and target-1 levels.
  * Detect externally-closed positions (user sold via broker UI).
  * Force-exit on severe adverse LLM news.
"""

from __future__ import annotations

import logging
from dataclasses import replace as _replace
from typing import Callable

from alpha_bot.approval import ApprovalQueue
from alpha_bot.broker.base import Broker
from alpha_bot.data import DataProvider
from alpha_bot.market_hours import market_status
from alpha_bot.models import (
    AnalysisReport,
    Market,
    OrderCandidate,
    OrderRequest,
)

logger = logging.getLogger(__name__)

_OPEN_STATUSES = {"pending", "submitted", "partially_filled", "filled"}


def count_open_positions(queue: ApprovalQueue) -> int:
    """Pending intent + still-working + filled (and not yet fully exited)
    count toward the max_positions cap. Cancelled/rejected and fully-exited
    buys do not."""
    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    seen: set[tuple[str, str]] = set()
    count = 0
    for order in orders:
        if order.request.side != "buy":
            continue
        if order.status not in _OPEN_STATUSES:
            continue
        exit_order = by_id.get(order.exit_order_id or "")
        if exit_order and exit_order.status == "filled":
            continue  # Already exited.
        key = (order.request.market, order.request.ticker)
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


# ── Force-exit signal extraction ─────────────────────────────────────


def should_force_exit(report: AnalysisReport) -> tuple[bool, str]:
    """Return ``(True, reason)`` if the analysis indicates we should exit
    a held position regardless of price levels.

    Triggers:
      * LLM detected severe negative news (severity=high, sentiment=negative)
      * earnings_caution flag from LLM or local fundamentals (EPS/매출 YoY < 0)
    """

    na = report.news_assessment
    if na and na.severity == "high" and na.sentiment == "negative":
        return True, f"심각한 악재(LLM): {na.reasoning or '뉴스 톤 매우 부정적'}"
    if report.earnings_caution:
        return True, "earnings_caution 플래그 (실적 둔화/하향 또는 LLM 경고)"
    return False, ""


def find_held_buy(queue: ApprovalQueue, market: Market, ticker: str) -> OrderCandidate | None:
    """Return any active buy order for the ticker — filled, partially filled,
    or still in-flight (submitted/pending). This prevents the auto-pilot from
    opening a second position while the first order is still working."""
    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    for buy in orders:
        if buy.request.side != "buy":
            continue
        if buy.request.market != market or buy.request.ticker != ticker:
            continue
        # In-flight buy (not yet confirmed by broker): treat as held to block re-entry.
        if buy.status in {"pending", "submitted"}:
            return buy
        if buy.status not in {"filled", "partially_filled"}:
            continue
        if (buy.filled_quantity or 0) <= 0:
            continue
        exit_order = by_id.get(buy.exit_order_id or "")
        if exit_order and exit_order.status in {
            "pending", "submitted", "partially_filled", "filled",
        }:
            continue
        return buy
    return None


def trigger_forced_exit(
    queue: ApprovalQueue,
    broker: Broker,
    buy: OrderCandidate,
    reason_label: str,
    detail: str,
    say: Callable[[str], None],
) -> None:
    qty = buy.filled_quantity
    market = buy.request.market
    ticker = buy.request.ticker
    say(f"  🚨 {market}:{ticker} {reason_label} → 시장가 매도 {qty}주 ({detail})")
    try:
        sell = queue.enqueue(
            OrderRequest(
                ticker=ticker,
                market=market,
                side="sell",
                quantity=qty,
                order_type="market",
                limit_price=None,
                reason=f"{reason_label}: {detail}",
            ),
            stop_loss=buy.stop_loss,
            target1=buy.target1,
            target2=buy.target2,
            analysis_signal="Sell",
        )
    except Exception as exc:
        say(f"    ❌ 매도 큐잉 실패: {exc}")
        return
    try:
        queue.update(_replace(buy, exit_order_id=sell.id, exit_reason=reason_label))
    except Exception as exc:
        logger.warning("Failed to link buy %s to forced exit %s: %s", buy.id, sell.id, exc)
    try:
        approved, result = queue.approve(sell.id, broker)
    except Exception as exc:
        say(f"    ❌ 매도 전송 실패: {exc}")
        return
    if approved.status == "submitted":
        say(f"    ✅ 매도 접수 (id={approved.id}, broker_ref={result.broker_order_id})")
    else:
        say(f"    ❌ 매도 거부: {result.message}")


# ── Queue ↔ broker reconciliation ─────────────────────────────────────


def reconcile_queue_with_broker(
    queue: ApprovalQueue,
    broker: Broker,
) -> list[OrderCandidate]:
    """Mark bot-held positions as externally closed when the broker disagrees.

    Walks every filled buy in the queue that doesn't already have a completed
    exit, groups by (market, ticker), and queries the broker for actual
    positions. If the broker reports 0 quantity for a ticker the bot still
    considers held, all stale buys for that ticker get a synthetic ``filled``
    sell appended so downstream logic treats them as closed.

    Returns the list of buys that were reconciled this run.
    """
    if not hasattr(broker, "get_positions"):
        return []

    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    holdings: dict[tuple[str, str], list[OrderCandidate]] = {}

    for o in orders:
        if o.request.side != "buy":
            continue
        if o.status not in {"filled", "partially_filled"}:
            continue
        if (o.filled_quantity or 0) <= 0:
            continue
        exit_o = by_id.get(o.exit_order_id or "")
        if exit_o and exit_o.status in {
            "pending", "submitted", "partially_filled", "filled",
        }:
            continue  # Already in flight or closed.
        holdings.setdefault((o.request.market, o.request.ticker), []).append(o)

    if not holdings:
        return []

    broker_qty: dict[tuple[str, str], int] = {}
    for market in {key[0] for key in holdings}:
        try:
            positions = broker.get_positions(market)  # type: ignore[arg-type]
            for p in positions:
                broker_qty[(market, p.ticker)] = p.quantity
        except Exception as exc:
            logger.warning("Reconcile: positions query failed for %s: %s", market, exc)

    reconciled: list[OrderCandidate] = []
    for (market, ticker), buy_list in holdings.items():
        if broker_qty.get((market, ticker), 0) > 0:
            continue  # Broker still holds; nothing to reconcile.
        for buy in buy_list:
            try:
                queue.mark_externally_closed(buy.id, broker_name=broker.name)
                reconciled.append(buy)
            except Exception as exc:
                logger.warning("Failed to mark %s externally closed: %s", buy.id, exc)
    return reconciled


# ── Position management (stop-loss / target take-profit) ─────────────


def manage_open_positions(
    queue: ApprovalQueue,
    broker: Broker,
    provider: DataProvider,
    say: Callable[[str], None],
) -> None:
    """For each filled buy without an active exit, check current price against
    stop_loss/target1 and fire a sell order if a level is breached."""

    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    price_cache: dict[tuple[str, str], float] = {}
    market_cache: dict[str, bool] = {}
    # Snapshot broker positions per market once — sells are validated against
    # this map so we never queue a sell for a ticker the broker doesn't actually
    # hold (would result in a rejection and stale "submitted" entries).
    broker_pos_cache: dict[str, dict[str, int]] = {}

    def _broker_qty(market: str, ticker: str) -> int | None:
        """Return broker-side quantity, or None if positions query failed."""
        if market not in broker_pos_cache:
            try:
                positions = broker.get_positions(market)  # type: ignore[arg-type]
                broker_pos_cache[market] = {p.ticker: p.quantity for p in positions}
            except Exception as exc:
                logger.warning("Position snapshot failed for %s: %s", market, exc)
                broker_pos_cache[market] = {}
                return None
        return broker_pos_cache[market].get(ticker, 0)

    for buy in orders:
        if buy.request.side != "buy":
            continue
        if buy.status not in {"filled", "partially_filled"}:
            continue
        if (buy.filled_quantity or 0) <= 0:
            continue
        if buy.avg_fill_price is None:
            # No confirmed fill price — skip to avoid acting on ghost positions.
            continue

        # Skip if there's already an in-flight or completed sell.
        active_exit = by_id.get(buy.exit_order_id or "")
        if active_exit and active_exit.status in {
            "pending", "submitted", "partially_filled", "filled",
        }:
            continue

        ticker = buy.request.ticker
        market = buy.request.market
        if market not in market_cache:
            status = market_status(market)
            market_cache[market] = status.is_open
            if not status.is_open:
                say(f"🌙 {market} {status.reason} — 보유 포지션 청산 평가 보류")
        if not market_cache[market]:
            continue
        key = (market, ticker)
        if key not in price_cache:
            try:
                candles = provider.get_candles(ticker, market, lookback=220)
            except Exception as exc:
                logger.warning("Price lookup failed for %s:%s: %s", market, ticker, exc)
                continue
            if not candles:
                continue
            price_cache[key] = candles[-1].close
        current = price_cache[key]

        trigger: str | None = None
        order_type: str = "limit"
        limit_price: float | None = round(current, 2)
        if buy.stop_loss is not None and current <= buy.stop_loss:
            trigger = "stop_loss"
            order_type = "market"
            limit_price = None  # Market order on hard stop.
        elif buy.target1 is not None and current >= buy.target1:
            trigger = "target1"
        if not trigger:
            continue

        qty = buy.filled_quantity

        # ── Verify broker actually has the position before queuing a sell. ──
        # If the user manually liquidated the position between iterations the
        # broker will show 0 qty; selling here would be rejected and leave a
        # stale "rejected" record. Reconcile silently and skip.
        actual_qty = _broker_qty(market, ticker)
        if actual_qty == 0:
            try:
                queue.mark_externally_closed(buy.id, broker_name=broker.name)
                say(
                    f"  🔄 {market}:{ticker} 외부 매도 감지 (브로커 보유 0) "
                    f"— 청산 트리거 무시"
                )
            except Exception as exc:
                logger.warning("Failed to mark %s externally closed: %s", buy.id, exc)
            continue
        if actual_qty is not None and actual_qty < qty:
            # Partial manual exit — sell only what the broker still holds.
            say(
                f"  ⚠️ {market}:{ticker} 봇 기록 {qty}주 vs 브로커 보유 {actual_qty}주 "
                f"— 실보유 수량으로 조정"
            )
            qty = actual_qty
        if qty <= 0:
            continue

        reason = (
            f"{trigger} 트리거 · 현재가 {current:.2f} "
            f"(stop={buy.stop_loss}, t1={buy.target1})"
        )
        say(
            f"  💰 {market}:{ticker} {trigger} → 매도 주문 {qty}주 "
            f"(현재 {current:.2f})"
        )
        try:
            sell = queue.enqueue(
                OrderRequest(
                    ticker=ticker,
                    market=market,
                    side="sell",
                    quantity=qty,
                    order_type=order_type,  # type: ignore[arg-type]
                    limit_price=limit_price,
                    reason=reason,
                ),
                stop_loss=buy.stop_loss,
                target1=buy.target1,
                target2=buy.target2,
                analysis_signal="Sell",
            )
        except Exception as exc:
            say(f"    ❌ 매도 큐잉 실패: {exc}")
            continue

        try:
            queue.update(_replace(buy, exit_order_id=sell.id, exit_reason=trigger))
        except Exception as exc:
            logger.warning("Failed to link buy %s to exit %s: %s", buy.id, sell.id, exc)

        try:
            approved, result = queue.approve(sell.id, broker)
        except Exception as exc:
            say(f"    ❌ 매도 전송 실패: {exc}")
            continue
        if approved.status == "submitted":
            say(
                f"    ✅ 매도 접수 (id={approved.id}, "
                f"broker_ref={result.broker_order_id})"
            )
        else:
            say(f"    ❌ 매도 거부: {result.message}")
