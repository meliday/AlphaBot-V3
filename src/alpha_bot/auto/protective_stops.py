"""Keep a broker-side stop in sync with the polling exit ladder.

Every stop in this bot used to be soft: it only existed as a comparison
inside ``manage_open_positions``, so a dead process meant an unprotected
position. Venues that can watch a price themselves (Toss conditional
orders) let us mirror the *currently effective* stop server-side, which
keeps working when the bot does not.

Division of labour, deliberately narrow:

  * The venue owns **one** thing — a market sell of the whole remaining
    position if price reaches the effective stop.
  * The bot keeps owning targets, the target-1 scale-out, the trail
    ratchet, and every news-driven exit.

Only the stop is mirrored because a Toss conditional order carries a
single quantity for the whole group; it cannot express "half at target-1,
the rest at target-2". Splitting responsibilities any further would mean
two engines racing to sell the same shares.

The invariant that makes this safe: **at most one seller at a time.**
Before the bot submits any sell of its own it releases the venue stop, and
while the venue stop is engaged the bot submits nothing. A brief unarmed
window during a bot-initiated exit is the accepted cost — the alternative
is overselling shares we do not hold.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace as _replace
from typing import Callable

from alpha_bot.approval import ApprovalQueue
from alpha_bot.broker.base import (
    PROTECTIVE_STOP_DEAD_STATUSES,
    PROTECTIVE_STOP_ENGAGED_STATUSES,
    Broker,
    supports_protective_stops,
)
from alpha_bot.models import OrderCandidate

logger = logging.getLogger(__name__)

# Relative tolerance when comparing the armed stop against the desired one.
# Without it, float noise in the ATR trail would re-send an identical stop on
# every sweep and burn the venue's rate limit.
_PRICE_EPSILON = 1e-6


def effective_stop_price(buy: OrderCandidate, *, scaled_out: bool) -> float | None:
    """The stop the ladder is enforcing right now.

    Mirrors ``_evaluate_exit``: before the target-1 scale-out the hard stop
    applies; after it the ratcheting trail governs the runner. Any change
    here must move in lockstep with that function or the venue would be
    protecting a different level than the bot believes.
    """

    if scaled_out:
        return buy.trail_stop if buy.trail_stop is not None else buy.stop_loss
    return buy.stop_loss


def _client_order_id(buy: OrderCandidate, quantity: int, stop_price: float) -> str:
    """Stable idempotency key for one (position, quantity, price) intent.

    Re-sending the same desired stop replays the same key, so a lost
    response cannot create a second stop inside the venue's idempotency
    window. A genuinely different stop derives a different key.
    """

    digest = hashlib.sha256(
        f"{buy.id}|{quantity}|{stop_price:.6f}".encode()
    ).hexdigest()[:10]
    return f"ps-{buy.id}-{digest}"[:36]


def _matches_armed_state(
    buy: OrderCandidate, quantity: int, stop_price: float
) -> bool:
    if buy.protective_stop_id is None:
        return False
    if buy.protective_stop_quantity != quantity:
        return False
    armed = buy.protective_stop_price
    if armed is None:
        return False
    return abs(armed - stop_price) <= max(_PRICE_EPSILON, abs(stop_price) * _PRICE_EPSILON)


def _forget(queue: ApprovalQueue, buy: OrderCandidate) -> OrderCandidate:
    """Drop local memory of an armed stop (venue-side already gone)."""

    updated = _replace(
        buy,
        protective_stop_id=None,
        protective_stop_price=None,
        protective_stop_quantity=0,
    )
    try:
        queue.update(updated)
    except Exception as exc:  # bookkeeping must never break trading
        logger.warning("Failed to clear protective stop state on %s: %s", buy.id, exc)
    return updated


def stop_engaged(
    broker: Broker, buy: OrderCandidate, say: Callable[[str], None]
) -> bool:
    """True when the venue stop has fired and is producing its own exit.

    The polling ladder must stand down in that case, otherwise both the
    venue and the bot sell the same shares.

    Fails **closed**: if we cannot determine the status we report engaged,
    because submitting a duplicate sell is worse than delaying one cycle.
    """

    if not buy.protective_stop_id or not supports_protective_stops(broker):
        return False
    try:
        status = broker.protective_stop_status(buy.protective_stop_id)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning(
            "Protective stop status unknown for %s (%s) — deferring bot exits: %s",
            buy.id, buy.protective_stop_id, exc,
        )
        say(
            f"  ⏳ {buy.request.market}:{buy.request.ticker} 조건주문 상태 확인 실패 "
            f"— 중복 매도 방지를 위해 이번 사이클 청산 보류"
        )
        return True
    if status in PROTECTIVE_STOP_ENGAGED_STATUSES:
        say(
            f"  🛡️ {buy.request.market}:{buy.request.ticker} 브로커 조건주문 발동 "
            f"({status}) — 봇 청산 보류, 체결 동기화 대기"
        )
        return True
    return False


def release_protective_stop(
    queue: ApprovalQueue,
    broker: Broker,
    buy: OrderCandidate,
    say: Callable[[str], None],
) -> tuple[OrderCandidate, bool]:
    """Cancel the venue stop before the bot sells, or once the position closes.

    Returns ``(buy, released_ok)``. A failed cancellation returns False so
    the caller can abort its own sell: leaving both alive risks selling more
    shares than the account holds.
    """

    if not buy.protective_stop_id:
        return buy, True
    if not supports_protective_stops(broker):
        return buy, True
    stop_id = buy.protective_stop_id
    try:
        broker.cancel_protective_stop(stop_id)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("Failed to cancel protective stop %s: %s", stop_id, exc)
        say(
            f"  ⚠️ {buy.request.market}:{buy.request.ticker} 조건주문 취소 실패 "
            f"({stop_id}) — 중복 매도 위험이 있어 이번 청산은 보류합니다: {exc}"
        )
        return buy, False
    return _forget(queue, buy), True


def sync_protective_stop(
    queue: ApprovalQueue,
    broker: Broker,
    buy: OrderCandidate,
    *,
    quantity: int,
    scaled_out: bool,
    say: Callable[[str], None],
    enabled: bool = True,
) -> OrderCandidate:
    """Arm, re-arm, or leave the venue stop alone for one held position.

    No-ops when disabled, unsupported, or already matching the desired
    state, so a steady-state sweep costs zero write calls. Never raises:
    a venue problem degrades protection back to polling-only, which is
    where this bot started, but it must not stop the loop from managing
    the position.
    """

    if not enabled or not supports_protective_stops(broker):
        return buy

    stop_price = effective_stop_price(buy, scaled_out=scaled_out)
    market = buy.request.market
    ticker = buy.request.ticker

    if stop_price is None or stop_price <= 0 or quantity <= 0:
        # Nothing meaningful to protect — make sure nothing stale is armed.
        if buy.protective_stop_id:
            buy, _ = release_protective_stop(queue, broker, buy, say)
        return buy

    # Reconcile local memory with the venue before deciding anything.
    if buy.protective_stop_id:
        try:
            status = broker.protective_stop_status(buy.protective_stop_id)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning(
                "Protective stop status check failed for %s: %s", buy.id, exc
            )
            return buy  # Unknown venue state: change nothing this pass.
        if status is None or status in PROTECTIVE_STOP_DEAD_STATUSES:
            say(
                f"  ♻️ {market}:{ticker} 조건주문 소멸 감지 "
                f"({status or 'not-found'}) — 재등록합니다"
            )
            buy = _forget(queue, buy)

    if _matches_armed_state(buy, quantity, stop_price):
        return buy

    try:
        if buy.protective_stop_id:
            new_id = broker.amend_protective_stop(  # type: ignore[attr-defined]
                buy.protective_stop_id,
                ticker=ticker,
                market=market,
                quantity=quantity,
                stop_price=stop_price,
            )
            action = "수정"
        else:
            new_id = broker.place_protective_stop(  # type: ignore[attr-defined]
                ticker=ticker,
                market=market,
                quantity=quantity,
                stop_price=stop_price,
                client_order_id=_client_order_id(buy, quantity, stop_price),
            )
            action = "등록"
    except Exception as exc:
        logger.warning("Protective stop sync failed for %s:%s: %s", market, ticker, exc)
        say(
            f"  ⚠️ {market}:{ticker} 브로커 손절 {('수정' if buy.protective_stop_id else '등록')} 실패 "
            f"— 폴링 손절로만 보호됩니다: {exc}"
        )
        return buy

    updated = _replace(
        buy,
        protective_stop_id=new_id,
        protective_stop_price=float(stop_price),
        protective_stop_quantity=int(quantity),
    )
    try:
        queue.update(updated)
    except Exception as exc:
        # The venue stop exists but we failed to record it. Cancel rather than
        # orphan it — an unreferenced stop would later sell shares that the
        # bot has already exited through another path.
        logger.warning(
            "Armed protective stop %s but failed to persist it on %s: %s",
            new_id, buy.id, exc,
        )
        try:
            broker.cancel_protective_stop(new_id)  # type: ignore[attr-defined]
            say(f"  ⚠️ {market}:{ticker} 조건주문 상태 저장 실패 → 등록분 롤백 완료")
        except Exception as cancel_exc:
            say(
                f"  🚨 {market}:{ticker} 조건주문 {new_id} 이 브로커에 남아있는데 "
                f"봇이 추적하지 못합니다 — 수동 확인 필요: {cancel_exc}"
            )
        return buy

    say(
        f"  🛡️ {market}:{ticker} 브로커 손절 {action} — {quantity}주 @ {stop_price:.2f} "
        f"(id={new_id})"
    )
    return updated
