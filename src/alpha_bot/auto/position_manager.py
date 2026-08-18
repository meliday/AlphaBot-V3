"""Position lifecycle management — stop/target monitoring, reconciliation, force-exit.

Handles the post-fill side of the order lifecycle:
  * Check held positions against stop-loss / target levels.
  * Scale out half at target-1, then trail the runner half (ATR ratchet,
    floored at breakeven) until target-2 or the trailing stop is hit.
  * Detect externally-closed positions (user sold via broker UI).
  * Force-exit on severe adverse LLM news (price-confirmed for the softer
    earnings_caution flag).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import replace as _replace
from typing import Callable

from alpha_bot.approval import ApprovalQueue
from alpha_bot.approval.queue import order_belongs_to_broker
from alpha_bot.broker.base import Broker
from alpha_bot.data import DataProvider
from alpha_bot.market_hours import market_status
from alpha_bot.models import (
    AnalysisReport,
    Market,
    OrderCandidate,
    OrderRequest,
)
from alpha_bot.notify import notify
from alpha_bot.risk import TRAIL_ATR_MULT as _TRAIL_ATR_MULT
from alpha_bot.strategy.indicators import latest_atr

logger = logging.getLogger(__name__)

# Order-status vocabulary used throughout this module:
#   working  — accepted but not fully done (may still fill or change)
#   active   — working ∪ filled: an exit in any of these states means the
#              position must not receive another sell.
_WORKING_STATUSES = {"pending", "submitting", "unknown", "submitted", "partially_filled"}
_ACTIVE_STATUSES = _WORKING_STATUSES | {"partially_filled_cancelled", "filled"}
_OPEN_STATUSES = _ACTIVE_STATUSES  # buys: pending intent through held


def remaining_quantity(
    buy: OrderCandidate, by_id: dict[str, OrderCandidate]
) -> tuple[int, bool]:
    """Return ``(shares still held, partial-exit in flight?)`` for a filled buy.

    Partial exits (target-1 scale-outs) reduce the position without closing
    it. Filled partials subtract their filled quantity; in-flight partials
    subtract only what has filled so far *and* raise the in-flight flag so
    callers defer new sell decisions until the next broker sync resolves
    the working order (prevents double-selling the same shares).
    """
    remaining = buy.filled_quantity or 0
    inflight = False

    def apply_sell(sell: OrderCandidate | None) -> None:
        nonlocal remaining, inflight
        if sell is None:
            return
        if sell.status == "filled":
            remaining -= sell.filled_quantity or sell.request.quantity
        elif sell.status == "partially_filled_cancelled":
            remaining -= sell.filled_quantity or 0
        elif sell.status in _WORKING_STATUSES:
            inflight = True
            remaining -= sell.filled_quantity or 0

    for pid in buy.partial_exit_ids:
        apply_sell(by_id.get(pid))
    apply_sell(by_id.get(buy.exit_order_id or ""))
    return max(remaining, 0), inflight


def _has_completed_scale_out(
    buy: OrderCandidate, by_id: dict[str, OrderCandidate]
) -> bool:
    for partial_id in buy.partial_exit_ids:
        partial = by_id.get(partial_id)
        if partial is None:
            continue
        if partial.filled_quantity > 0:
            return True
        if partial.status == "filled" and partial.request.quantity > 0:
            return True
    return False


def count_open_positions(queue: ApprovalQueue, broker: Broker | None = None) -> int:
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
        if broker is not None:
            # Unbound pending intents are conservatively counted because they
            # may still be approved into this account. Broker-active legacy
            # rows without a scope are handled separately and never traded.
            if order.status != "pending" and not order_belongs_to_broker(order, broker):
                continue
            if order.status == "pending" and order.broker_instance_id \
                    and not order_belongs_to_broker(order, broker):
                continue
        if order.status not in _OPEN_STATUSES:
            continue
        if order.status in {"filled", "partially_filled", "partially_filled_cancelled"}:
            remaining, inflight = remaining_quantity(order, by_id)
            if remaining <= 0 and not inflight:
                continue
        key = (order.request.market, order.request.ticker)
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


# ── Force-exit signal extraction ─────────────────────────────────────


def should_force_exit(report: AnalysisReport) -> tuple[bool, str]:
    """Return ``(True, reason)`` if the analysis indicates we should exit
    a held position regardless of stop/target levels.

    Triggers:
      * LLM detected severe negative news (severity=high, sentiment=negative)
        → immediate, no price confirmation (emergency path).
      * earnings_caution (LLM or local fundamentals, EPS/매출 YoY < 0)
        → only when price CONFIRMS the concern by trading below the 50-day
        SMA. A single soft quarter or one cautious LLM read must not
        market-liquidate a position the market still supports; if price
        holds the 50-day line we keep the position and let the normal
        stop/target logic manage it.
    """

    na = report.news_assessment
    if na and na.severity == "high" and na.sentiment == "negative":
        return True, f"심각한 악재(LLM): {na.reasoning or '뉴스 톤 매우 부정적'}"
    if report.earnings_caution:
        ind = report.indicators
        if ind.close < ind.sma50:
            return True, (
                "earnings_caution + 50일선 이탈 확인 "
                f"(close {ind.close:.2f} < SMA50 {ind.sma50:.2f})"
            )
        logger.info(
            "%s:%s earnings_caution present but price above SMA50 — holding",
            report.market, report.ticker,
        )
    return False, ""


def find_held_buy(
    queue: ApprovalQueue,
    market: Market,
    ticker: str,
    broker: Broker | None = None,
) -> OrderCandidate | None:
    """Return any active buy order for the ticker — filled, partially filled,
    or still in-flight (submitted/pending). This prevents the auto-pilot from
    opening a second position while the first order is still working."""
    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    for buy in orders:
        if buy.request.side != "buy":
            continue
        if broker is not None and not order_belongs_to_broker(buy, broker):
            continue
        if buy.request.market != market or buy.request.ticker != ticker:
            continue
        # In-flight buy (not yet confirmed by broker): treat as held to block re-entry.
        if buy.status in {"pending", "submitting", "unknown", "submitted"}:
            return buy
        if buy.status not in {"filled", "partially_filled", "partially_filled_cancelled"}:
            continue
        if (buy.filled_quantity or 0) <= 0:
            continue
        remaining, inflight = remaining_quantity(buy, by_id)
        if remaining > 0 or inflight:
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
    by_id = {o.id: o for o in queue.list_orders()}
    qty, inflight = remaining_quantity(buy, by_id)
    market = buy.request.market
    ticker = buy.request.ticker
    if inflight:
        say(
            f"  ⏳ {market}:{ticker} 부분 매도 체결 대기 중 — "
            f"강제 청산은 다음 사이클에 재시도"
        )
        return
    if qty <= 0:
        return
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
            broker=broker,
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
        notify(
            f"🚨 AlphaBot 강제 청산: {market}:{ticker} {qty}주 시장가 매도\n"
            f"{reason_label}: {detail}",
            dedupe_key=f"force_exit:{market}:{ticker}:{sell.id}",
        )
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
        if not order_belongs_to_broker(o, broker):
            continue
        if o.status not in {"filled", "partially_filled", "partially_filled_cancelled"}:
            continue
        if (o.filled_quantity or 0) <= 0:
            continue
        remaining, inflight = remaining_quantity(o, by_id)
        if inflight or remaining <= 0:
            continue
        holdings.setdefault((o.request.market, o.request.ticker), []).append(o)

    if not holdings:
        return []

    broker_qty: dict[tuple[str, str], int] = {}
    successful_markets: set[str] = set()
    for market in {key[0] for key in holdings}:
        try:
            positions = broker.get_positions(market)  # type: ignore[arg-type]
            for p in positions:
                broker_qty[(market, p.ticker)] = p.quantity
            successful_markets.add(market)
        except Exception as exc:
            logger.warning("Reconcile: positions query failed for %s: %s", market, exc)

    reconciled: list[OrderCandidate] = []
    for (market, ticker), buy_list in holdings.items():
        if market not in successful_markets:
            continue  # Unknown snapshot is never equivalent to zero holdings.
        if broker_qty.get((market, ticker), 0) > 0:
            continue  # Broker still holds; nothing to reconcile.
        for buy in buy_list:
            try:
                queue.mark_externally_closed(
                    buy.id, broker_name=broker.name, broker=broker
                )
                reconciled.append(buy)
            except Exception as exc:
                logger.warning("Failed to mark %s externally closed: %s", buy.id, exc)
    return reconciled


# ── Position management (stop-loss / target take-profit) ─────────────


@dataclass(frozen=True)
class _ExitDecision:
    """One resolved exit action for a held buy at the current price."""

    trigger: str               # stop_loss | target1 | trail_stop | target2
    order_type: str            # "market" (stops) | "limit" (targets)
    limit_price: float | None  # None for market orders
    scale_out: bool            # True → sell the larger half, keep a runner


def _evaluate_exit(
    buy: OrderCandidate,
    current: float,
    qty_held: int,
    *,
    scaled_out: bool | None = None,
) -> _ExitDecision | None:
    """Map (position state, current price) → exit action, or None to hold.

    Phase is derived from ``partial_exit_ids``: empty → full position under
    hard stop / target-1; non-empty → runner half under trailing stop /
    target-2. Pure decision logic — no I/O — so the ladder is testable
    without a queue or broker.
    """
    if scaled_out is None:
        scaled_out = bool(buy.partial_exit_ids)
    if scaled_out:
        effective_stop = buy.trail_stop if buy.trail_stop is not None else buy.stop_loss
        if effective_stop is not None and current <= effective_stop:
            return _ExitDecision("trail_stop", "market", None, False)
        if buy.target2 is not None and current >= buy.target2:
            return _ExitDecision("target2", "limit", round(current, 2), False)
        return None
    if buy.stop_loss is not None and current <= buy.stop_loss:
        return _ExitDecision("stop_loss", "market", None, False)
    if buy.target1 is not None and current >= buy.target1:
        # A single share can't be split — qty 1 exits in full at target-1.
        return _ExitDecision("target1", "limit", round(current, 2), qty_held > 1)
    return None


def _ratchet_trail(
    queue: ApprovalQueue,
    buy: OrderCandidate,
    by_id: dict[str, OrderCandidate],
    candles: list,
    current: float,
    say: Callable[[str], None],
) -> None:
    """Raise the runner's trailing stop; never lowers, floored at breakeven."""
    atr14 = latest_atr(candles, 14)
    candidate = max(current - _TRAIL_ATR_MULT * atr14, buy.avg_fill_price)
    if buy.trail_stop is not None and candidate <= buy.trail_stop:
        return
    updated = _replace(buy, trail_stop=round(candidate, 4))
    try:
        queue.update(updated)
        by_id[buy.id] = updated
        say(
            f"  📈 {buy.request.market}:{buy.request.ticker} 트레일링 스톱 상향 → "
            f"{candidate:.2f} (현재 {current:.2f})"
        )
    except Exception as exc:
        logger.warning("Trail update failed for %s: %s", buy.id, exc)


def _submit_exit(
    queue: ApprovalQueue,
    broker: Broker,
    buy: OrderCandidate,
    by_id: dict[str, OrderCandidate],
    decision: _ExitDecision,
    qty: int,
    qty_held: int,
    current: float,
    candles: list,
    say: Callable[[str], None],
) -> None:
    """Enqueue → link → approve one exit sell (partial scale-out or final)."""
    market = buy.request.market
    ticker = buy.request.ticker
    reason = (
        f"{decision.trigger} 트리거 · 현재가 {current:.2f} "
        f"(stop={buy.stop_loss}, t1={buy.target1}, t2={buy.target2}, "
        f"trail={buy.trail_stop})"
    )
    label = (
        f"{decision.trigger} 분할매도 {qty}/{qty_held}주"
        if decision.scale_out
        else f"{decision.trigger} → 매도 주문 {qty}주"
    )
    say(f"  💰 {market}:{ticker} {label} (현재 {current:.2f})")
    try:
        sell = queue.enqueue(
            OrderRequest(
                ticker=ticker,
                market=market,
                side="sell",
                quantity=qty,
                order_type=decision.order_type,  # type: ignore[arg-type]
                limit_price=decision.limit_price,
                reason=reason,
            ),
            broker=broker,
            stop_loss=buy.stop_loss,
            target1=buy.target1,
            target2=buy.target2,
            analysis_signal="Sell",
        )
    except Exception as exc:
        say(f"    ❌ 매도 큐잉 실패: {exc}")
        return

    if decision.scale_out:
        # Partial exit: link via partial_exit_ids and arm the trail for the
        # runner half — breakeven floor guarantees the remaining shares can
        # no longer turn the whole trade into a loss.
        atr14 = latest_atr(candles, 14)
        initial_trail = max(current - _TRAIL_ATR_MULT * atr14, buy.avg_fill_price)
        updated = _replace(
            buy,
            partial_exit_ids=[*buy.partial_exit_ids, sell.id],
            trail_stop=round(initial_trail, 4),
        )
    else:
        updated = _replace(buy, exit_order_id=sell.id, exit_reason=decision.trigger)
    try:
        queue.update(updated)
        by_id[buy.id] = updated
    except Exception as exc:
        logger.warning("Failed to link buy %s to exit %s: %s", buy.id, sell.id, exc)

    try:
        approved, result = queue.approve(sell.id, broker)
    except Exception as exc:
        say(f"    ❌ 매도 전송 실패: {exc}")
        return
    if approved.status == "submitted":
        say(
            f"    ✅ 매도 접수 (id={approved.id}, "
            f"broker_ref={result.broker_order_id})"
        )
        notify(
            f"💰 AlphaBot 청산: {market}:{ticker} {qty}주 ({decision.trigger}) "
            f"@ {current:.2f} — 진입 {buy.avg_fill_price:.2f}",
            dedupe_key=f"exit:{market}:{ticker}:{sell.id}",
        )
    else:
        say(f"    ❌ 매도 거부: {result.message}")


def manage_open_positions(
    queue: ApprovalQueue,
    broker: Broker,
    provider: DataProvider,
    say: Callable[[str], None],
) -> None:
    """For each filled buy without a completed exit, walk the exit ladder:

      * before target-1: hard stop (market-sell everything) or target-1
        scale-out (limit-sell the larger half; trail armed at breakeven or
        close − 2×ATR, whichever is higher);
      * after target-1: trailing stop (market-sell the runner) or target-2
        (limit-sell the runner), with the trail ratcheting up every pass.
    """

    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    candle_cache: dict[tuple[str, str], list] = {}
    live_price_cache: dict[tuple[str, str], float | None] = {}
    market_cache: dict[str, bool] = {}
    # Snapshot broker positions per market once — sells are validated against
    # this map so we never queue a sell for a ticker the broker doesn't actually
    # hold (would result in a rejection and stale "submitted" entries).
    broker_pos_cache: dict[str, dict[str, int]] = {}
    broker_pos_failed: set[str] = set()

    def _broker_qty(market: str, ticker: str) -> int | None:
        """Return broker-side quantity, or None if positions query failed."""
        if market in broker_pos_failed:
            return None
        if market not in broker_pos_cache:
            try:
                positions = broker.get_positions(market)  # type: ignore[arg-type]
                broker_pos_cache[market] = {p.ticker: p.quantity for p in positions}
            except Exception as exc:
                logger.warning("Position snapshot failed for %s: %s", market, exc)
                broker_pos_failed.add(market)
                return None
        return broker_pos_cache[market].get(ticker, 0)

    for buy in orders:
        if buy.request.side != "buy":
            continue
        if not order_belongs_to_broker(buy, broker):
            continue
        if buy.status not in {"filled", "partially_filled", "partially_filled_cancelled"}:
            continue
        if (buy.filled_quantity or 0) <= 0:
            continue
        if buy.avg_fill_price is None:
            # No confirmed fill price — skip to avoid acting on ghost positions.
            continue

        # Net out all confirmed sells, including a terminal partial final
        # exit; defer only while a sell is genuinely still working.
        qty_held, inflight = remaining_quantity(buy, by_id)
        if inflight:
            continue
        if qty_held <= 0:
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
        if key not in candle_cache:
            try:
                fetched = provider.get_candles(ticker, market, lookback=220)
            except Exception as exc:
                logger.warning("Price lookup failed for %s:%s: %s", market, ticker, exc)
                continue
            if not fetched:
                continue
            candle_cache[key] = fetched
        candles = candle_cache[key]
        # Prefer a live quote for trigger checks — the last daily candle can
        # lag an intraday move by hours. ATR/trail math still uses candles.
        if key not in live_price_cache:
            live = None
            if hasattr(provider, "get_current_price"):
                try:
                    live = provider.get_current_price(ticker, market)
                except Exception as exc:
                    logger.debug("Live quote failed for %s:%s: %s", market, ticker, exc)
            live_price_cache[key] = live
        current = live_price_cache[key] or candles[-1].close

        scaled_out = _has_completed_scale_out(buy, by_id)
        decision = _evaluate_exit(
            buy, current, qty_held, scaled_out=scaled_out
        )
        if decision is None:
            # Runner with no exit hit: keep tightening the trail.
            if scaled_out:
                _ratchet_trail(queue, buy, by_id, candles, current, say)
            continue

        qty = (qty_held + 1) // 2 if decision.scale_out else qty_held

        # ── Verify broker actually has the position before queuing a sell. ──
        # If the user manually liquidated the position between iterations the
        # broker will show 0 qty; selling here would be rejected and leave a
        # stale "rejected" record. Reconcile silently and skip.
        actual_qty = _broker_qty(market, ticker)
        if actual_qty == 0:
            try:
                queue.mark_externally_closed(
                    buy.id, broker_name=broker.name, broker=broker
                )
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

        _submit_exit(
            queue, broker, buy, by_id, decision,
            qty, qty_held, current, candles, say,
        )
