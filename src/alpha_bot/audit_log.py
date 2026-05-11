"""Structured append-only audit log.

All events are JSONL-appended to the `logs/` directory (created on first use).

  logs/activity_YYYYMMDD.jsonl  — query / queue / trade / cash events
  logs/llm_YYYYMMDD.jsonl       — full LLM prompt + raw response

Each line is a self-contained JSON object:
  {"ts": "2026-05-11T12:34:56+00:00", "event": "<type>", ...payload}
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_DIR = Path("logs")
LOG_DIR = _LOG_DIR  # public alias for read-only consumers (web stats, etc.)
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _append(filename: str, record: dict[str, Any]) -> None:
    """Thread-safe JSONL append."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _LOG_DIR / filename
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:
        logger.warning("audit_log write failed (%s): %s", filename, exc)


# ── Public helpers ────────────────────────────────────────────────────

def log_llm(
    ticker: str,
    market: str,
    model: str,
    news_text: str,
    raw_response: str,
    parsed: dict[str, Any],
) -> None:
    """Log the full LLM prompt input and raw JSON response."""
    _append(
        f"llm_{_today_str()}.jsonl",
        {
            "ts": _now_iso(),
            "event": "llm_assessment",
            "ticker": ticker,
            "market": market,
            "model": model,
            "news_length": len(news_text),
            "news_snippet": news_text[:300] if news_text else "",
            "raw_response": raw_response,
            "parsed": parsed,
        },
    )


def log_query(
    ticker: str,
    market: str,
    signal: str,
    score: int,
    rr: float,
    reason: str,
    source: str = "unknown",
    news_sentiment: str | None = None,
    news_adjustment: int | None = None,
) -> None:
    """Log a completed analysis (조회 내역)."""
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": "query",
        "ticker": ticker,
        "market": market,
        "signal": signal,
        "score": score,
        "rr": round(rr, 3),
        "reason": reason,
        "source": source,
    }
    if news_sentiment is not None:
        record["news_sentiment"] = news_sentiment
    if news_adjustment is not None:
        record["news_adjustment"] = news_adjustment
    _append(f"activity_{_today_str()}.jsonl", record)


def log_queue(
    order_id: str,
    ticker: str,
    market: str,
    side: str,
    quantity: int,
    limit_price: float | None,
    signal: str | None,
    stop_loss: float | None = None,
    target1: float | None = None,
) -> None:
    """Log an order being added to the approval queue (큐잉 내역)."""
    total = round(limit_price * quantity, 4) if limit_price else None
    _append(
        f"activity_{_today_str()}.jsonl",
        {
            "ts": _now_iso(),
            "event": "queue",
            "order_id": order_id,
            "ticker": ticker,
            "market": market,
            "side": side,
            "quantity": quantity,
            "limit_price": limit_price,
            "total_value": total,
            "signal": signal,
            "stop_loss": stop_loss,
            "target1": target1,
        },
    )


def log_trade(
    order_id: str,
    ticker: str,
    market: str,
    side: str,
    status: str,
    quantity: int,
    fill_price: float | None = None,
    fill_qty: int | None = None,
    broker_order_id: str | None = None,
    broker_message: str | None = None,
    cash_balance: float | None = None,
    cash_currency: str | None = None,
) -> None:
    """Log a trade event: submit / fill / reject (거래 내역 + 예수금)."""
    total_filled = round(fill_price * fill_qty, 4) if (fill_price and fill_qty) else None
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": "trade",
        "order_id": order_id,
        "ticker": ticker,
        "market": market,
        "side": side,
        "status": status,
        "quantity": quantity,
        "fill_price": fill_price,
        "fill_qty": fill_qty,
        "total_filled": total_filled,
        "broker_order_id": broker_order_id,
        "broker_message": broker_message,
    }
    if cash_balance is not None:
        record["cash_balance"] = cash_balance
        record["cash_currency"] = cash_currency
    _append(f"activity_{_today_str()}.jsonl", record)


def log_cash_snapshot(
    market: str,
    broker: str,
    cash: float,
    currency: str,
    total_value: float | None = None,
    trigger: str = "after_trade",
) -> None:
    """Log current cash (예수금) and portfolio value snapshot."""
    _append(
        f"activity_{_today_str()}.jsonl",
        {
            "ts": _now_iso(),
            "event": "cash_snapshot",
            "market": market,
            "broker": broker,
            "cash": cash,
            "currency": currency,
            "total_value": total_value,
            "trigger": trigger,
        },
    )
