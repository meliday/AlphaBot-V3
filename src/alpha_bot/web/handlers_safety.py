"""One call that answers "is my money safe right now?".

Everything here already existed — kill switch, daily-loss breaker, the
legacy/unresolved order halts, heartbeats, armed protective stops — but
only ever surfaced as log lines. So the dashboard could not distinguish
"the bot is quiet because nothing qualifies" from "the bot is halted and
has been for hours", which is the single most important question once
real money is involved.

Design rules:

* **Never raise.** This endpoint powers a status bar on every screen; a
  broker hiccup must degrade one field, not blank the bar. Every probe is
  individually wrapped and reports ``unknown`` on failure.
* **``unknown`` is not ``ok``.** A field we could not read renders as a
  warning, because silently showing green for something unverified is the
  exact failure mode this endpoint exists to prevent.
* **Read-only.** No syncing, no arming, no cancelling. Polled every few
  seconds, so it must also stay cheap: broker calls are limited to the
  markets the bot actually holds positions in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alpha_bot.approval import ApprovalQueue
from alpha_bot.config import load_config

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")

# The auto loop writes a heartbeat each sweep (default 300s interval) and
# the tick monitor far more often. Three missed auto sweeps is a real
# outage; anything less is normal jitter.
_AUTO_HEARTBEAT_MAX_AGE = 900.0
_MONITOR_HEARTBEAT_MAX_AGE = 120.0


def _probe(name: str, fn, default: Any = None) -> Any:
    """Run one probe, converting any failure into a reported unknown."""

    try:
        return fn()
    except Exception as exc:
        logger.warning("Safety probe %r failed: %s", name, exc)
        return default


def handle_safety() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    queue = ApprovalQueue(
        config.approval_queue, protected_tickers=config.protected_tickers
    )

    result: dict[str, Any] = {
        "broker": config.broker,
        "protective_stop_enabled": config.protective_stop,
        "protected_tickers": sorted(config.protected_tickers),
    }

    # ── Kill switch (a file; cheap and always answerable) ──
    def kill_switch() -> dict[str, Any]:
        from alpha_bot.auto.guards import kill_switch_active
        reason = kill_switch_active()
        return {"active": bool(reason), "reason": reason or ""}

    result["kill_switch"] = _probe(
        "kill_switch", kill_switch, {"active": None, "reason": "확인 실패"}
    )

    # ── Heartbeats ──
    def heartbeats() -> dict[str, Any]:
        from alpha_bot.auto.watchdog import check_heartbeat
        out: dict[str, Any] = {}
        for component, max_age in (
            ("auto", _AUTO_HEARTBEAT_MAX_AGE),
            ("monitor", _MONITOR_HEARTBEAT_MAX_AGE),
        ):
            health = check_heartbeat(component, max_age)
            out[component] = {
                "healthy": health.healthy,
                "detail": health.reason,
                "age_seconds": round(health.age_seconds, 1)
                if health.age_seconds is not None else None,
            }
        return out

    result["heartbeats"] = _probe("heartbeats", heartbeats, {})

    # ── Queue-derived state (no broker needed) ──
    def positions() -> dict[str, Any]:
        from alpha_bot.auto.position_manager import remaining_quantity

        orders = queue.list_orders()
        by_id = {o.id: o for o in orders}
        held, armed, pending_intent = [], 0, 0
        for order in orders:
            if order.request.side != "buy":
                continue
            if order.status not in {"filled", "partially_filled", "partially_filled_cancelled"}:
                continue
            if (order.filled_quantity or 0) <= 0:
                continue
            remaining, inflight = remaining_quantity(order, by_id)
            if remaining <= 0 and not inflight:
                continue
            if order.protective_stop_id:
                armed += 1
            elif order.protective_stop_quantity > 0:
                # Intent persisted but the venue id never came back — the
                # write-ahead recovery case. Neither armed nor unprotected.
                pending_intent += 1
            held.append({
                "id": order.id,
                "ticker": order.request.ticker,
                "market": order.request.market,
                "quantity": remaining,
                "avg_fill_price": order.avg_fill_price,
                "stop_loss": order.stop_loss,
                "trail_stop": order.trail_stop,
                "protective_stop_id": order.protective_stop_id,
                "protective_stop_price": order.protective_stop_price,
            })
        return {
            "held": held,
            "count": len(held),
            "armed": armed,
            "pending_intent": pending_intent,
        }

    result["positions"] = _probe(
        "positions", positions,
        {"held": [], "count": None, "armed": None, "pending_intent": None},
    )

    # ── Broker-dependent probes ──
    # Only the markets we actually hold, so an idle bot costs nothing.
    held = (result.get("positions") or {}).get("held") or []
    markets = sorted({p["market"] for p in held}) or []

    def broker_state() -> dict[str, Any]:
        from alpha_bot.auto.analysis import make_broker
        from alpha_bot.auto.guards import daily_loss_exceeded

        broker = make_broker(config.broker)
        out: dict[str, Any] = {
            "legacy_orders": len(queue.unscoped_broker_orders(broker)),
            "unresolved_orders": len(queue.unresolved_orders(broker)),
        }

        breakers: dict[str, Any] = {}
        for market in markets:
            tripped, detail = daily_loss_exceeded(
                queue, broker, market, config.daily_loss_limit_pct
            )
            breakers[market] = {"tripped": tripped, "detail": detail}
        out["breakers"] = breakers

        # Account value in KRW — the base sizing and the breaker both use.
        if hasattr(broker, "portfolio_value"):
            out["portfolio_krw"] = round(broker.portfolio_value("KRW"), 2)
        return out

    result["broker_state"] = _probe(
        "broker_state", broker_state,
        {
            "legacy_orders": None, "unresolved_orders": None,
            "breakers": {}, "portfolio_krw": None,
        },
    )

    result["overall"] = _overall(result)
    return result


def _overall(state: dict[str, Any]) -> dict[str, Any]:
    """Collapse the fields into one banner verdict.

    Severity ordering is deliberate: ``halted`` means new buys are blocked
    right now, ``warn`` means something needs a human look but trading
    continues, ``ok`` means every probe answered and answered well.
    """

    halted: list[str] = []
    warn: list[str] = []

    kill = state.get("kill_switch") or {}
    if kill.get("active") is True:
        reason = kill.get("reason") or ""
        halted.append(f"킬스위치 활성 ({reason})" if reason else "킬스위치 활성")
    elif kill.get("active") is None:
        warn.append("킬스위치 상태 확인 실패")

    broker_state = state.get("broker_state") or {}
    legacy = broker_state.get("legacy_orders")
    unresolved = broker_state.get("unresolved_orders")
    if legacy:
        halted.append(f"계좌 미귀속 주문 {legacy}건")
    if unresolved:
        halted.append(f"결과 불명 주문 {unresolved}건")
    if legacy is None or unresolved is None:
        warn.append("주문 귀속 상태 확인 실패")

    for market, breaker in (broker_state.get("breakers") or {}).items():
        if breaker.get("tripped"):
            halted.append(f"{market} 일일손실 한도")

    positions = state.get("positions") or {}
    if state.get("protective_stop_enabled"):
        count, armed = positions.get("count"), positions.get("armed")
        if count is not None and armed is not None and count > armed:
            # Expected transiently (a position arms on the next sweep) but
            # worth surfacing: these shares have no venue-side stop yet.
            warn.append(f"손절 미무장 {count - armed}건")
        if positions.get("pending_intent"):
            warn.append(f"조건주문 결과 미확인 {positions['pending_intent']}건")

    for component, health in (state.get("heartbeats") or {}).items():
        if not health.get("healthy"):
            # A stopped bot is a normal state, not an alarm — only flag a
            # heartbeat that exists but went stale.
            if "missing" not in str(health.get("detail", "")):
                warn.append(f"{component} 하트비트 이상")

    if halted:
        return {"level": "halted", "reasons": halted + warn}
    if warn:
        return {"level": "warn", "reasons": warn}
    return {"level": "ok", "reasons": []}


# ── Gates: "why isn't the bot buying anything?" ──────────────────────
#
# The single most common question about a running bot, and until now it
# was answerable only by reading log lines as they scrolled past. Each
# check below mirrors an actual guard in run_auto_iteration, in the same
# order, so the panel explains the real decision path rather than an
# approximation of it.
#
# Costlier than /api/safety (regime and breaker probes hit the network,
# though both sit behind caches), so the UI polls it on a slower beat.

def handle_gates() -> dict[str, Any]:
    from alpha_bot.config import load_watchlist

    config = load_config(CONFIG_PATH)
    queue = ApprovalQueue(
        config.approval_queue, protected_tickers=config.protected_tickers
    )

    watchlist_path = Path("watchlist.yaml")
    rows = _probe(
        "watchlist",
        lambda: load_watchlist(watchlist_path) if watchlist_path.exists() else [],
        [],
    )

    result: dict[str, Any] = {
        "watchlist_file": str(watchlist_path) if watchlist_path.exists() else None,
        "max_positions": config.max_positions,
    }

    # ── Capacity (queue-only, always answerable) ──
    def capacity() -> dict[str, Any]:
        from alpha_bot.auto.position_manager import count_open_positions
        open_count = count_open_positions(queue)
        return {
            "open": open_count,
            "max": config.max_positions,
            "full": open_count >= config.max_positions,
        }

    result["capacity"] = _probe(
        "capacity", capacity,
        {"open": None, "max": config.max_positions, "full": None},
    )

    markets = sorted({str(r.get("market", "US")).upper() for r in rows}) or ["US"]

    def market_gates() -> dict[str, Any]:
        from alpha_bot.auto.analysis import make_broker
        from alpha_bot.auto.guards import daily_loss_exceeded
        from alpha_bot.auto.sizing import usable_cash
        from alpha_bot.market_hours import market_status
        from alpha_bot.market_regime import get_regime

        broker = _probe("gates_broker", lambda: make_broker(config.broker))
        out: dict[str, Any] = {}
        for market in markets:
            entry: dict[str, Any] = {}

            status = _probe(f"session:{market}", lambda m=market: market_status(m))
            entry["session"] = (
                {"open": status.is_open, "reason": status.reason}
                if status else {"open": None, "reason": "확인 실패"}
            )

            regime = _probe(f"regime:{market}", lambda m=market: get_regime(m))
            entry["regime"] = (
                {"bullish": regime.is_bullish, "reason": regime.reason}
                if regime else {"bullish": None, "reason": "확인 실패"}
            )

            if broker is not None:
                breaker = _probe(
                    f"breaker:{market}",
                    lambda m=market: daily_loss_exceeded(
                        queue, broker, m, config.daily_loss_limit_pct
                    ),
                )
                entry["breaker"] = (
                    {"tripped": breaker[0], "detail": breaker[1]}
                    if breaker else {"tripped": None, "detail": "확인 실패"}
                )
                balance = _probe(
                    f"cash:{market}", lambda m=market: broker.get_cash_balance(m)
                )
                entry["cash"] = (
                    {"available": round(usable_cash(balance), 2),
                     "currency": balance.currency}
                    if balance else {"available": None, "currency": ""}
                )
            else:
                entry["breaker"] = {"tripped": None, "detail": "브로커 없음"}
                entry["cash"] = {"available": None, "currency": ""}

            out[market] = entry
        return out

    result["markets"] = _probe("market_gates", market_gates, {})

    # ── Per-ticker blockers, in the order the sweep applies them ──
    def tickers() -> list[dict[str, Any]]:
        from alpha_bot.auto.position_manager import find_held_buy

        gates = result.get("markets") or {}
        out: list[dict[str, Any]] = []
        for row in rows:
            ticker = str(row.get("ticker", "")).upper()
            market = str(row.get("market", "US")).upper()
            gate = gates.get(market) or {}
            blocked: str | None = None

            if ticker in config.protected_tickers:
                blocked = "보호 종목"
            elif (result.get("capacity") or {}).get("full"):
                blocked = "포지션 상한 도달"
            elif gate.get("session", {}).get("open") is False:
                blocked = "장 마감"
            elif gate.get("regime", {}).get("bullish") is False:
                blocked = "시장 레짐 약세"
            elif gate.get("breaker", {}).get("tripped"):
                blocked = "일일손실 한도"
            else:
                held = _probe(
                    f"held:{ticker}",
                    lambda t=ticker, m=market: find_held_buy(queue, m, t),
                )
                if held is not None:
                    blocked = "이미 보유/주문중"

            out.append({
                "ticker": ticker, "market": market,
                "company": row.get("company", ticker),
                "blocked_by": blocked,
            })
        return out

    result["tickers"] = _probe("tickers", tickers, [])
    return result
