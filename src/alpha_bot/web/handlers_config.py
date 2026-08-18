"""Configuration, settings, watchlist, account, logs, and exchange-rate handlers."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from alpha_bot.auto import make_broker
from alpha_bot.config import load_config, load_watchlist
from alpha_bot.data.quotes import fetch_quotes
from alpha_bot.utils import validate_market
from alpha_bot.web.autopilot_state import auto_pilot

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")
ENV_PATH = Path(".env")
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


# ── .env read/write helpers ──────────────────────────────────────────


def _read_env_safe() -> dict[str, str]:
    """Return .env values with secrets masked for safe display."""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value and any(hint in key.upper() for hint in _SECRET_HINTS):
            value = (value[:4] + "***") if len(value) > 4 else "***"
        out[key] = value
    return out


def _write_env_partial(updates: dict[str, str]) -> None:
    """Write the given keys back to .env, preserving comments and other lines."""
    existing = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    written: set[str] = set()
    output: list[str] = []
    for raw in existing:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(raw)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            output.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            output.append(raw)
    for key, value in updates.items():
        if key not in written:
            output.append(f"{key}={value}")
    temp_path = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
    temp_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, ENV_PATH)
    os.chmod(ENV_PATH, 0o600)


# ── Handler functions ────────────────────────────────────────────────


def handle_config(serialise: Any) -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    return serialise(config)


def handle_settings(serialise: Any) -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    return {"config": serialise(config), "env": _read_env_safe()}


def handle_save_settings(body: dict[str, Any]) -> dict[str, Any] | tuple[str, int]:
    env = body.get("env") or {}
    if not isinstance(env, dict):
        return ("env must be an object", 400)
    allowed = {
        "KIS_MODE", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
        "KIS_ACCOUNT_PRODUCT", "KIS_HTS_ID", "BOT_BROKER",
        "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ",
        "TOSS_ENABLE_LIVE_ORDERS", "TOSS_TOKEN_CACHE",
        "BOT_APPROVAL_QUEUE", "OPENAI_API_KEY", "OPENAI_MODEL",
    }
    updates = {k: str(v) for k, v in env.items() if k in allowed}
    updates = {k: v for k, v in updates.items() if "***" not in v}
    if any(any(char in value for char in ("\n", "\r", "\0")) for value in updates.values()):
        return ("env values cannot contain newlines or null bytes", 400)
    if "TOSS_ACCOUNT_SEQ" in updates and updates["TOSS_ACCOUNT_SEQ"]:
        if not updates["TOSS_ACCOUNT_SEQ"].isdigit() or int(updates["TOSS_ACCOUNT_SEQ"]) <= 0:
            return ("TOSS_ACCOUNT_SEQ must be a positive integer", 400)
    if updates.get("TOSS_ENABLE_LIVE_ORDERS", "false").lower() not in {
        "true", "false", "1", "0", "yes", "no", "on", "off",
    }:
        return ("TOSS_ENABLE_LIVE_ORDERS must be true or false", 400)
    _write_env_partial(updates)
    return {"saved": list(updates.keys())}


def handle_auto_start(body: dict[str, Any]) -> dict[str, Any] | tuple[str, int]:
    if auto_pilot.active:
        return ("Auto-pilot is already running", 400)
    started = auto_pilot.start(
        watchlist=body.get("watchlist", "watchlist.example.yaml"),
        interval=int(body.get("interval", 300)),
        broker=body.get("broker", "mock"),
        quantity=int(body.get("quantity", 1)),
        source=body.get("source", "toss"),
        use_llm=bool(body.get("use_llm", True)),
        cooldown_hours=int(body.get("cooldown_hours", 24)),
        cooldown_enabled=bool(body.get("cooldown_enabled", True)),
        auto_size=bool(body.get("auto_size", False)),
    )
    if not started:
        return ("Auto-pilot is already running", 400)
    return {"status": "started"}


def handle_auto_stop() -> dict[str, Any]:
    auto_pilot.stop()
    return {"status": "stopping"}


def handle_auto_status() -> dict[str, Any]:
    return auto_pilot.status()


def handle_watchlists() -> list[str]:
    roots = [Path("."), Path.cwd()]
    seen: set[str] = set()
    files: list[str] = []
    for root in roots:
        for ext in ("*.yaml", "*.yml", "*.json"):
            for path in sorted(root.glob(ext)):
                if "watchlist" in path.name.lower() and path.name not in seen:
                    seen.add(path.name)
                    files.append(str(path))
    if not files:
        files = ["watchlist.example.yaml"]
    return files


def handle_watchlist_load(params: dict[str, str]) -> dict[str, Any]:
    file_name = params.get("file", "watchlist.yaml")
    path = Path(file_name)
    if not path.exists():
        path = Path.cwd() / file_name
    if not path.exists():
        return {"file": file_name, "tickers": []}
    rows = load_watchlist(path)
    return {"file": file_name, "tickers": rows}


def handle_watchlist_save(body: dict[str, Any]) -> dict[str, Any]:
    import os
    file_name = str(body.get("file", "watchlist.yaml"))
    tickers: list[dict] = body.get("tickers", [])
    path = Path(file_name)
    if not path.is_absolute():
        path = Path.cwd() / file_name
    lines = ["universe:\n"]
    for t in tickers:
        lines.append(f"  - ticker: {t['ticker'].upper()}\n")
        lines.append(f"    market: {t['market'].upper()}\n")
        if t.get("company"):
            lines.append(f"    company: {t['company']}\n")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(path)
    return {"saved": file_name, "count": len(tickers)}


def handle_quotes(params: dict[str, str]) -> list[dict]:
    file_name = params.get("file", "watchlist.yaml")
    path = Path(file_name)
    if not path.exists():
        path = Path.cwd() / file_name
    tickers = load_watchlist(path) if path.exists() else []
    return fetch_quotes(tickers)


def handle_account(params: dict[str, str], serialise: Any) -> dict[str, Any]:
    broker_name = params.get("broker", "toss")
    market_param = params.get("market", "ALL").upper()
    broker = make_broker(broker_name)
    markets: list[str] = ["KR", "US"] if market_param == "ALL" else [market_param]
    out: dict[str, Any] = {"broker": broker_name, "markets": {}}
    for market in markets:
        try:
            balance = broker.get_cash_balance(market)  # type: ignore[arg-type]
            positions = broker.get_positions(market)  # type: ignore[arg-type]
            out["markets"][market] = {
                "balance": serialise(balance),
                "positions": [serialise(p) for p in positions],
                "error": None,
            }
        except Exception as exc:
            logger.warning("Account query failed for %s/%s: %s", broker_name, market, exc)
            out["markets"][market] = {"balance": None, "positions": [], "error": str(exc)}
    return out


# ── Exchange-rate helpers ─────────────────────────────────────────────

_RATE_CACHE: dict[str, Any] = {}
_RATE_TTL = 3600.0


def handle_exchange_rate() -> dict[str, Any]:
    return _fetch_exchange_rate()


def _fetch_exchange_rate() -> dict[str, Any]:
    """Return USD/KRW rate, cached for 1 hour. Fail-soft: returns None rate."""
    if _RATE_CACHE and time.time() - _RATE_CACHE.get("ts", 0) < _RATE_TTL:
        return _RATE_CACHE
    rate: float | None = None
    try:
        import yfinance as yf
        ticker = yf.Ticker("KRW=X")
        rate = float(ticker.fast_info.last_price)
    except Exception:
        try:
            import yfinance as yf
            hist = yf.Ticker("KRW=X").history(period="2d", interval="1d")
            if not hist.empty:
                rate = float(hist["Close"].iloc[-1])
        except Exception:
            pass
    from alpha_bot.models import utc_now_iso
    result: dict[str, Any] = {"rate": rate, "as_of": utc_now_iso()}
    _RATE_CACHE.update(result)
    _RATE_CACHE["ts"] = time.time()
    return result


# ── Log / report handlers ────────────────────────────────────────────


def handle_logs(params: dict[str, str]) -> dict[str, Any]:
    from alpha_bot.audit_log import _LOG_DIR
    date_str = params.get("date", "")
    event_type = params.get("type", "")
    ticker = params.get("ticker", "").upper()
    try:
        limit = int(params.get("limit", "200"))
    except ValueError:
        limit = 200
    if not date_str:
        from alpha_bot.daily_report import available_dates
        dates = available_dates()
        date_str = dates[0] if dates else ""
    records: list[dict] = []
    if event_type != "llm_assessment":
        path = _LOG_DIR / f"activity_{date_str}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and r.get("event") != event_type:
                    continue
                if ticker and r.get("ticker", "").upper() != ticker:
                    continue
                records.append(r)
    if not event_type or event_type == "llm_assessment":
        path = _LOG_DIR / f"llm_{date_str}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ticker and r.get("ticker", "").upper() != ticker:
                    continue
                records.append(r)
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    from alpha_bot.daily_report import available_dates
    return {
        "date": date_str,
        "total": len(records),
        "records": records[:limit],
        "available_dates": available_dates(),
    }


def handle_daily_report(params: dict[str, str]) -> dict[str, Any]:
    from alpha_bot.daily_report import build_daily_summary
    date_str = params.get("date") or None
    return build_daily_summary(date_str)
