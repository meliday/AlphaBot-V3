"""Portfolio, bot holdings, and bot stats web API handlers."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from alpha_bot.approval import ApprovalQueue
from alpha_bot.config import load_config
from alpha_bot.data.quotes import fetch_quotes

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")


def handle_portfolio(serialise: Any) -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    queue = ApprovalQueue(config.approval_queue)
    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}

    # ── Open positions (filled buys without a completed exit) ──
    held: list[dict[str, Any]] = []
    for o in orders:
        if o.request.side != "buy":
            continue
        if o.status not in ("filled", "partially_filled"):
            continue
        exit_o = by_id.get(o.exit_order_id or "")
        if exit_o and exit_o.status == "filled":
            continue  # position is closed
        qty = o.filled_quantity or o.request.quantity
        avg = o.avg_fill_price
        if not avg:
            continue
        held.append({
            "ticker": o.request.ticker,
            "market": o.request.market,
            "company": "",
            "quantity": qty,
            "avg_price": avg,
            "order_id": o.id,
            "broker": o.broker,
        })

    quotes = fetch_quotes(held) if held else []
    open_positions = []
    for h, q in zip(held, quotes):
        price = q["price"]
        avg = h["avg_price"]
        pnl = (price - avg) * h["quantity"] if price else None
        pnl_pct = (price / avg - 1) * 100 if price and avg else None
        open_positions.append({
            **q,
            "quantity": h["quantity"],
            "avg_price": avg,
            "order_id": h["order_id"],
            "broker": h["broker"],
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        })

    # ── Closed trades (filled buy + filled exit) ──
    closed_trades = []
    for buy in orders:
        if buy.request.side != "buy":
            continue
        if (buy.filled_quantity or 0) <= 0 or not buy.avg_fill_price:
            continue
        exit_o = by_id.get(buy.exit_order_id or "")
        if not exit_o or exit_o.status != "filled":
            continue
        entry = buy.avg_fill_price
        exit_price = exit_o.avg_fill_price or exit_o.request.limit_price
        if not exit_price:
            continue
        qty = buy.filled_quantity
        ret_pct = (exit_price / entry - 1) * 100
        pnl = (exit_price - entry) * qty
        closed_trades.append({
            "ticker": buy.request.ticker,
            "market": buy.request.market,
            "broker": buy.broker,
            "entry": entry,
            "exit": exit_price,
            "quantity": qty,
            "pnl": pnl,
            "return_pct": ret_pct,
            "entry_date": buy.submitted_at or buy.created_at,
            "exit_date": exit_o.submitted_at or exit_o.created_at,
            "exit_reason": buy.exit_reason or "",
        })

    brokers = sorted({p["broker"] for p in open_positions} | {t["broker"] for t in closed_trades})
    return {
        "open": open_positions,
        "closed": closed_trades,
        "brokers": brokers,
    }


def handle_bot_holdings(params: dict[str, str]) -> dict[str, Any]:
    """Return ONLY bot-purchased positions, cross-checked with broker state.

    This is the canonical source for the Mission-Control UI: it tells us
    which tickers the bot is currently managing (i.e. has a filled buy
    without a completed exit) and pairs each with live price + stop/target
    for visualisation. Manual user-held positions in the broker do not
    appear here — those should be viewed via /api/account.
    """
    config = load_config(CONFIG_PATH)
    queue = ApprovalQueue(config.approval_queue)
    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}

    # Collect bot-held positions (filled buys without completed exit)
    held: list[dict[str, Any]] = []
    for o in orders:
        if o.request.side != "buy":
            continue
        if o.status not in ("filled", "partially_filled"):
            continue
        if (o.filled_quantity or 0) <= 0:
            continue
        exit_o = by_id.get(o.exit_order_id or "")
        if exit_o and exit_o.status == "filled":
            continue
        held.append({
            "ticker": o.request.ticker,
            "market": o.request.market,
            "company": "",
            "quantity": o.filled_quantity,
            "avg_price": o.avg_fill_price,
            "stop_loss": o.stop_loss,
            "target1": o.target1,
            "target2": o.target2,
            "order_id": o.id,
            "broker": o.broker,
            "submitted_at": o.submitted_at or o.created_at,
            "has_active_exit": bool(exit_o and exit_o.status in {"pending", "submitted", "partially_filled"}),
        })

    if not held:
        return {"positions": [], "count": 0}

    quotes = fetch_quotes(held)
    result = []
    for h, q in zip(held, quotes):
        price = q.get("price")
        avg = h["avg_price"]
        pnl = (price - avg) * h["quantity"] if price and avg else None
        pnl_pct = (price / avg - 1) * 100 if price and avg else None
        # Position of price on the stop→target ladder (0..100 %)
        ladder_pct = None
        if price and h.get("stop_loss") and h.get("target1"):
            span = h["target1"] - h["stop_loss"]
            if span > 0:
                raw = (price - h["stop_loss"]) / span * 100
                ladder_pct = max(0.0, min(100.0, raw))
        result.append({
            **h,
            "price": price,
            "company": q.get("company") or h["ticker"],
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "ladder_pct": ladder_pct,
        })
    return {"positions": result, "count": len(result)}


def handle_bot_stats() -> dict[str, Any]:
    """Return today's bot activity counts: scans, signals, orders, fills.

    Derived from the audit log JSONL written under ``logs/``. Counts cover
    the local-calendar-day so the UI shows "today's" activity.
    """
    from datetime import datetime
    from alpha_bot.audit_log import LOG_DIR

    today = datetime.now().strftime("%Y-%m-%d")
    path = LOG_DIR / f"{today}.jsonl"
    stats = {"scans": 0, "signals": 0, "orders": 0, "fills": 0}
    if path.exists():
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ev = rec.get("event")
                if ev == "query":
                    stats["scans"] += 1
                    if rec.get("signal") in ("Buy", "Strong Buy"):
                        stats["signals"] += 1
                elif ev == "queue":
                    stats["orders"] += 1
                elif ev == "trade":
                    if rec.get("status") in ("filled", "partially_filled", "submitted"):
                        stats["fills"] += 1
        except Exception as exc:
            logger.warning("Bot stats read failed: %s", exc)
    return {"date": today, **stats}
