"""일일 활동 요약 리포트.

logs/activity_YYYYMMDD.jsonl 과 logs/llm_YYYYMMDD.jsonl 을 읽어
하루치 활동을 집계한다.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


_LOG_DIR = Path("logs")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def build_daily_summary(date_str: str | None = None) -> dict[str, Any]:
    """집계 결과를 dict 로 반환. date_str 은 'YYYYMMDD' 형식."""
    ds = date_str or _today_str()
    activity = _read_jsonl(_LOG_DIR / f"activity_{ds}.jsonl")
    llm_recs  = _read_jsonl(_LOG_DIR / f"llm_{ds}.jsonl")

    queries: list[dict] = []
    queued:  list[dict] = []
    trades:  list[dict] = []
    cash_snaps: list[dict] = []

    for r in activity:
        ev = r.get("event", "")
        if ev == "query":
            queries.append(r)
        elif ev == "queue":
            queued.append(r)
        elif ev == "trade":
            trades.append(r)
        elif ev == "cash_snapshot":
            cash_snaps.append(r)

    # ── 조회 집계 ─────────────────────────────────────────────
    signal_counts: dict[str, int] = defaultdict(int)
    ticker_signals: dict[str, list[str]] = defaultdict(list)
    for q in queries:
        sig = q.get("signal", "")
        signal_counts[sig] += 1
        ticker_signals[q.get("ticker", "")].append(sig)

    buy_tickers = [
        {"ticker": t, "signals": sigs}
        for t, sigs in ticker_signals.items()
        if any(s in {"Buy", "Strong Buy"} for s in sigs)
    ]

    # ── 큐잉 집계 ─────────────────────────────────────────────
    total_queued_value: float = 0.0
    queued_by_market: dict[str, int] = defaultdict(int)
    for q in queued:
        tv = q.get("total_value") or 0
        total_queued_value += float(tv)
        queued_by_market[q.get("market", "?")] += 1

    # ── 거래 집계 ─────────────────────────────────────────────
    trade_status_counts: dict[str, int] = defaultdict(int)
    for t in trades:
        trade_status_counts[t.get("status", "?")] += 1

    # ── 예수금 최신 스냅샷 ────────────────────────────────────
    last_cash: dict[str, Any] | None = cash_snaps[-1] if cash_snaps else None

    # ── LLM 집계 ──────────────────────────────────────────────
    llm_count = len(llm_recs)
    sentiment_counts: dict[str, int] = defaultdict(int)
    for r in llm_recs:
        parsed = r.get("parsed") or {}
        sentiment_counts[parsed.get("sentiment", "?")] += 1

    return {
        "date": ds,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "queries": {
            "total": len(queries),
            "by_signal": dict(signal_counts),
            "buy_tickers": buy_tickers,
        },
        "queued": {
            "total": len(queued),
            "by_market": dict(queued_by_market),
            "total_value": round(total_queued_value, 2),
            "items": queued,
        },
        "trades": {
            "total": len(trades),
            "by_status": dict(trade_status_counts),
            "items": trades,
        },
        "cash": last_cash,
        "llm": {
            "total_calls": llm_count,
            "by_sentiment": dict(sentiment_counts),
        },
    }


def available_dates() -> list[str]:
    """로그 디렉터리에서 activity_*.jsonl 기준으로 날짜 목록 반환 (최신순)."""
    if not _LOG_DIR.exists():
        return []
    dates = []
    for p in _LOG_DIR.glob("activity_*.jsonl"):
        ds = p.stem.replace("activity_", "")
        if len(ds) == 8 and ds.isdigit():
            dates.append(ds)
    return sorted(dates, reverse=True)
