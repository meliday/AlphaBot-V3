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

Two invariants drive every branch in this module:

**At most one seller.** Before the bot submits any sell of its own it
releases the venue stop, and while the venue stop is engaged (or merely
*unknown*) the bot submits nothing. A brief unarmed window during a
bot-initiated exit is the accepted cost — the alternative is overselling
shares we do not hold.

**Every venue write must be resumable.** Toss's conditional-order create
honours a ``clientOrderId`` idempotency key (10 minutes); its *modify*
endpoint does not, so modify is never used — a re-arm is cancel + create.
Before any create, the desired (price, quantity) is persisted with the
stop id still empty ("write-ahead intent"); the create key derives
deterministically from that pair, so after a lost response the next pass
replays the exact same request and recovers the venue's id instead of
arming a duplicate. The one unclosable hole — a replay landing after the
venue's idempotency window while the original also landed — is covered by
``warn_unreferenced_stops``, which detects venue stops the queue does not
reference. It only warns, never cancels: the venue's list response omits
``clientOrderId``, so a bot-orphan is indistinguishable from a stop the
operator placed by hand in the Toss app, and destroying the operator's
own protection would be worse than an alert.

Session coverage is asymmetric by venue rule, and both sides are accepted
deliberately. KR conditionals trigger during the KRX regular session only,
so this protects against bot downtime, not overnight gaps. US conditionals
trigger in **every** tradable session — a stop can fire as a market sell
into a thin 4 a.m. pre-market book. That is kept as-is: in a gap scenario
it is the only protection that exists, and the alternative (a limit stop)
fails precisely when it matters most, by gapping through unfilled.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
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

# Venue status lookups are cached briefly. The live monitor evaluates every
# ~2 seconds; without a cache each held position would cost thousands of
# CONDITIONAL_ORDER_HISTORY calls per hour just to hear "still WATCHING".
# 15s keeps the reaction to a fired stop inside one or two evaluation beats
# while cutting the call volume by ~90%.
_STATUS_TTL_SECONDS = 15.0
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: dict[str, tuple[float, str | None]] = {}


def reset_status_cache() -> None:
    with _STATUS_LOCK:
        _STATUS_CACHE.clear()


def _invalidate_status(stop_id: str | None) -> None:
    if not stop_id:
        return
    with _STATUS_LOCK:
        _STATUS_CACHE.pop(stop_id, None)


def _venue_status(broker: Broker, stop_id: str) -> str | None:
    """Cached ``protective_stop_status``. Exceptions propagate to callers,
    which each have their own fail-closed/open policy; only successful
    lookups are cached."""

    now = time.monotonic()
    with _STATUS_LOCK:
        hit = _STATUS_CACHE.get(stop_id)
        if hit and (now - hit[0]) < _STATUS_TTL_SECONDS:
            return hit[1]
    status = broker.protective_stop_status(stop_id)  # type: ignore[attr-defined]
    with _STATUS_LOCK:
        _STATUS_CACHE[stop_id] = (now, status)
    return status


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
    """Deterministic idempotency key for one (position, quantity, price) intent.

    Determinism is the recovery mechanism: a lost create response is
    replayed by simply re-deriving the key from the persisted intent, so
    the venue returns the already-created stop instead of a duplicate.
    """

    digest = hashlib.sha256(
        f"{buy.id}|{quantity}|{stop_price:.6f}".encode()
    ).hexdigest()[:10]
    return f"ps-{buy.id}-{digest}"[:36]


def _has_pending_intent(buy: OrderCandidate) -> bool:
    """A create was intended (and possibly sent) but its id never persisted."""

    return (
        buy.protective_stop_id is None
        and buy.protective_stop_price is not None
        and buy.protective_stop_quantity > 0
    )


def _matches_armed_state(
    buy: OrderCandidate, quantity: int, stop_price: float, tolerance: float
) -> bool:
    if buy.protective_stop_id is None:
        return False
    if buy.protective_stop_quantity != quantity:
        return False
    armed = buy.protective_stop_price
    if armed is None:
        return False
    slack = max(tolerance, _PRICE_EPSILON, abs(stop_price) * _PRICE_EPSILON)
    return abs(armed - stop_price) <= slack


def _persist(queue: ApprovalQueue, buy: OrderCandidate, **changes) -> OrderCandidate:
    updated = _replace(buy, **changes)
    queue.update(updated)
    return updated


def _forget(queue: ApprovalQueue, buy: OrderCandidate) -> OrderCandidate:
    """Drop local memory of an armed stop (venue-side already gone)."""

    _invalidate_status(buy.protective_stop_id)
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


def resolve_pending_stop(
    queue: ApprovalQueue,
    broker: Broker,
    buy: OrderCandidate,
    say: Callable[[str], None],
    *,
    quiet: bool = False,
) -> tuple[OrderCandidate, bool]:
    """Finish a create whose response was lost.

    Replays the identical request (same deterministic key), so within the
    venue's idempotency window this returns the id of the stop that already
    exists rather than arming a second one. Returns ``(buy, ok)``.

    Callers use the flag differently by risk: the manage loop treats a
    failure as "repair later" and keeps evaluating (holding and ratcheting
    are harmless), while ``release_protective_stop`` treats it as "a stop
    may exist" and aborts the bot's own sell — the only action that can
    violate the single-seller invariant. ``quiet`` suppresses the operator
    message for the harmless path so a venue outage doesn't spam every
    evaluation beat.
    """

    if not _has_pending_intent(buy) or not supports_protective_stops(broker):
        return buy, True
    try:
        stop_id = broker.place_protective_stop(  # type: ignore[attr-defined]
            ticker=buy.request.ticker,
            market=buy.request.market,
            quantity=buy.protective_stop_quantity,
            stop_price=buy.protective_stop_price,
            client_order_id=_client_order_id(
                buy, buy.protective_stop_quantity, buy.protective_stop_price
            ),
        )
    except Exception as exc:
        logger.warning("Pending protective stop unresolved for %s: %s", buy.id, exc)
        if not quiet:
            say(
                f"  ⏳ {buy.request.market}:{buy.request.ticker} 조건주문 등록 결과 미확인 "
                f"— 재확인 전까지 매도 판단 보류: {exc}"
            )
        return buy, False
    _invalidate_status(stop_id)
    try:
        buy = _persist(queue, buy, protective_stop_id=stop_id)
    except Exception as exc:
        # Intent stays on disk; the next pass replays the same key again.
        logger.warning("Failed to persist resolved stop id on %s: %s", buy.id, exc)
        return buy, False
    say(
        f"  🔁 {buy.request.market}:{buy.request.ticker} 조건주문 결과 복구 "
        f"(id={stop_id})"
    )
    return buy, True


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
        status = _venue_status(broker, buy.protective_stop_id)
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

    Returns ``(buy, released_ok)``. A failed cancellation — or an intent
    whose venue-side outcome is still unknown — returns False so the caller
    aborts its own sell: leaving two live sellers risks selling more shares
    than the account holds.
    """

    if not supports_protective_stops(broker):
        return buy, True
    if _has_pending_intent(buy):
        # The stop may exist at the venue without us holding its id. Resolve
        # first; only a known id can be cancelled with certainty.
        buy, ok = resolve_pending_stop(queue, broker, buy, say)
        if not ok:
            return buy, False
    if not buy.protective_stop_id:
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
    amend_tolerance: float = 0.0,
) -> OrderCandidate:
    """Arm or re-arm the venue stop for one held position.

    ``amend_tolerance`` (a price distance, typically a fraction of ATR)
    throttles trail-driven re-arms: the armed stop may lag the local trail
    by up to the tolerance, which slightly loosens the disaster brake but
    keeps a rallying runner from turning every evaluation into a venue
    cancel+create. A *quantity* change always re-arms regardless.

    No-ops when disabled, unsupported, or already matching; never raises —
    a venue problem degrades protection back to polling-only, which is
    where this bot started, but it must not stop the loop from managing
    the position.
    """

    if not enabled or not supports_protective_stops(broker):
        return buy

    buy, ok = resolve_pending_stop(queue, broker, buy, say)
    if not ok:
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
            status = _venue_status(broker, buy.protective_stop_id)
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

    if _matches_armed_state(buy, quantity, stop_price, amend_tolerance):
        return buy

    # Re-arm = cancel + idempotent create. Toss's modify endpoint has no
    # idempotency key, so a lost modify response would leave an untracked
    # stop; cancel+create with the write-ahead intent below is resumable at
    # every step.
    if buy.protective_stop_id:
        old_id = buy.protective_stop_id
        try:
            broker.cancel_protective_stop(old_id)  # type: ignore[attr-defined]
        except Exception as exc:
            # Old stop still armed — protection intact at the previous level.
            logger.warning("Protective stop cancel failed for %s: %s", buy.id, exc)
            say(
                f"  ⚠️ {market}:{ticker} 조건주문 갱신 실패 (기존 스톱 유지 "
                f"{buy.protective_stop_price}): {exc}"
            )
            return buy
        _invalidate_status(old_id)

    # Write-ahead intent: desired state hits disk before the network call,
    # so a lost create response is recoverable by replaying the same key.
    try:
        buy = _persist(
            queue, buy,
            protective_stop_id=None,
            protective_stop_price=float(stop_price),
            protective_stop_quantity=int(quantity),
        )
    except Exception as exc:
        logger.warning("Failed to persist stop intent on %s: %s", buy.id, exc)
        say(f"  ⚠️ {market}:{ticker} 조건주문 의도 기록 실패 — 다음 사이클 재시도")
        return buy

    try:
        stop_id = broker.place_protective_stop(  # type: ignore[attr-defined]
            ticker=ticker,
            market=market,
            quantity=quantity,
            stop_price=stop_price,
            client_order_id=_client_order_id(buy, quantity, float(stop_price)),
        )
    except Exception as exc:
        logger.warning("Protective stop create failed for %s:%s: %s", market, ticker, exc)
        say(
            f"  ⚠️ {market}:{ticker} 브로커 손절 등록 실패 — 폴링 손절로만 보호됩니다: {exc}"
        )
        return buy  # Intent persisted; next pass replays the same key.

    _invalidate_status(stop_id)
    try:
        buy = _persist(queue, buy, protective_stop_id=stop_id)
    except Exception as exc:
        logger.warning(
            "Armed protective stop %s but failed to persist it on %s "
            "(intent on disk — next pass recovers via idempotent replay): %s",
            stop_id, buy.id, exc,
        )
        return buy

    say(
        f"  🛡️ {market}:{ticker} 브로커 손절 무장 — {quantity}주 @ {stop_price:.2f} "
        f"(id={stop_id})"
    )
    return buy


def warn_unreferenced_stops(
    queue: ApprovalQueue,
    broker: Broker,
    say: Callable[[str], None],
) -> list[str]:
    """Report venue stops the queue does not know about. Warn-only.

    This is the safety net for the one recovery hole idempotent replay
    cannot close (a replay landing after the venue's idempotency window
    while the original also landed). It never cancels: the venue's list
    response carries no ``clientOrderId``, so an orphaned bot stop is
    indistinguishable from a conditional order the operator placed by hand
    in the Toss app — and cancelling the operator's own protection would
    be strictly worse than a loud alert.
    """

    lister = getattr(broker, "list_protective_stop_ids", None)
    if not callable(lister):
        return []

    orders = queue.list_orders()
    referenced = {o.protective_stop_id for o in orders if o.protective_stop_id}
    symbols = {
        (o.request.market, o.request.ticker)
        for o in orders
        if o.request.side == "buy"
        and (o.protective_stop_id or o.protective_stop_quantity > 0)
    }

    unknown: list[str] = []
    for market, ticker in sorted(symbols):
        try:
            venue_ids = set(lister(ticker))
        except Exception as exc:
            logger.warning("Stop listing failed for %s:%s: %s", market, ticker, exc)
            continue
        for stop_id in sorted(venue_ids - referenced):
            unknown.append(stop_id)
            say(
                f"  🚨 {market}:{ticker} 봇이 추적하지 않는 조건주문 발견 ({stop_id}) "
                f"— 토스 앱에서 직접 확인 필요 (봇 잔여물이면 수동 취소)"
            )
            try:
                from alpha_bot.notify import notify
                notify(
                    f"🚨 AlphaBot: {market}:{ticker} 미추적 조건주문 {stop_id}\n"
                    "봇이 등록한 잔여물일 수 있습니다. 토스 앱에서 확인하세요.",
                    dedupe_key=f"orphan_stop:{stop_id}",
                )
            except Exception:
                pass
    return unknown
