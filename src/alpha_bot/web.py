"""Web dashboard server for Alpha Strategy Bot.

Run with:
    python3 -m alpha_bot.web          # http://localhost:8501
    python3 -m alpha_bot.web --port 9000
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.parse
import threading
import time
from dataclasses import asdict, fields, is_dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_bot.approval import ApprovalQueue
from alpha_bot.data.quotes import fetch_quotes
from alpha_bot.auto import (
    AutoTradeOptions,
    analyze_ticker,
    make_broker,
    make_provider,
    run_auto_iteration,
)
from alpha_bot.backtest import Backtester
from alpha_bot.config import load_config, load_watchlist
from alpha_bot.errors import BotError
from alpha_bot.models import OrderRequest
from alpha_bot.report import render_report
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.utils import validate_market

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CONFIG_PATH = Path("config.yaml")

# ── Auto-pilot State ──────────────────────────────────────────────────
auto_pilot_thread = None
auto_pilot_active = False
auto_pilot_logs = []
auto_pilot_started_at: str | None = None
auto_pilot_interval: int = 0

def _log_auto(msg: str):
    timestamp = time.strftime('%H:%M:%S')
    auto_pilot_logs.append({"time": timestamp, "msg": msg})
    if len(auto_pilot_logs) > 100:
        auto_pilot_logs.pop(0)

def auto_pilot_loop(
    watchlist: str,
    interval: int,
    broker_name: str,
    quantity: int,
    source: str,
    use_llm: bool = True,
    cooldown_hours: int = 24,
    cooldown_enabled: bool = True,
    auto_size: bool = False,
):
    global auto_pilot_active
    config = load_config(CONFIG_PATH)
    opts = AutoTradeOptions(
        watchlist=Path(watchlist),
        broker_name=broker_name,
        quantity=quantity,
        source=source,
        language="ko",
        cooldown_hours=cooldown_hours,
        cooldown_enabled=cooldown_enabled,
        use_llm=use_llm,
        auto_size=auto_size,
    )

    _log_auto(
        f"🚀 자동매매 봇 시작 (브로커: {broker_name}, 감시주기: {interval}초, "
        f"LLM: {use_llm}, 자동 사이징: {auto_size})"
    )

    while auto_pilot_active:
        try:
            _log_auto(f"🔄 [{watchlist}] 관심종목 스캔 시작...")
            run_auto_iteration(opts, config, log=_log_auto)

            if auto_pilot_active:
                _log_auto(f"💤 스캔 완료. {interval}초 대기 중...")
                for _ in range(interval):
                    if not auto_pilot_active:
                        break
                    time.sleep(1)
        except Exception as exc:
            _log_auto(f"🛑 루프 오류 발생: {exc}")
            time.sleep(5)

    _log_auto("🛑 자동매매 봇이 중지되었습니다.")

# ── .env read/write helpers (used by /api/settings) ─────────────────

ENV_PATH = Path(".env")
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


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
    ENV_PATH.write_text("\n".join(output) + "\n", encoding="utf-8")


# ── Serialisation helpers ────────────────────────────────────────────

def _serialise(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, list):
        return [_serialise(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _serialise(getattr(obj, f.name)) for f in fields(obj)}
    return str(obj)


# ── HTTP Handler ─────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    """Serves the web dashboard and JSON API."""

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        logger.debug(fmt, *args)

    # ── GET routes ───────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = dict(urllib.parse.parse_qsl(parsed.query))

        routes: dict[str, Any] = {
            "/": self._serve_dashboard,
            "/api/analyze": lambda: self._handle_analyze(params),
            "/api/scan": lambda: self._handle_scan(params),
            "/api/orders": self._handle_orders,
            "/api/backtest": lambda: self._handle_backtest(params),
            "/api/config": self._handle_config,
            "/api/auto/status": self._handle_auto_status,
            "/api/watchlists": self._handle_watchlists,
            "/api/watchlist/load": lambda: self._handle_watchlist_load(params),
            "/api/quotes": lambda: self._handle_quotes(params),
            "/api/portfolio": self._handle_portfolio,
            "/api/settings": self._handle_settings,
            "/api/account": lambda: self._handle_account(params),
            "/api/exchange-rate": self._handle_exchange_rate,
            "/api/logs": lambda: self._handle_logs(params),
            "/api/daily-report": lambda: self._handle_daily_report(params),
            "/api/bot/holdings": lambda: self._handle_bot_holdings(params),
            "/api/bot/stats": self._handle_bot_stats,
            "/api/mock/state": self._handle_mock_state,
        }
        handler = routes.get(path)
        if handler:
            try:
                handler()
            except Exception as exc:
                self._json_error(str(exc), 500)
        else:
            self._json_error("Not found", 404)

    # ── POST routes ──────────────────────────────────────────────

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            
            if path == "/api/orders/approve":
                self._handle_approve(body)
            elif path == "/api/orders/queue":
                self._handle_queue(body)
            elif path == "/api/orders/sync":
                self._handle_sync_orders(body)
            elif path == "/api/auto/start":
                self._handle_auto_start(body)
            elif path == "/api/auto/stop":
                self._handle_auto_stop()
            elif path == "/api/settings":
                self._handle_save_settings(body)
            elif path == "/api/watchlist/save":
                self._handle_watchlist_save(body)
            elif path == "/api/mock/order":
                self._handle_mock_order(body)
            elif path == "/api/mock/set-cash":
                self._handle_mock_set_cash(body)
            elif path == "/api/mock/inject":
                self._handle_mock_inject(body)
            elif path == "/api/mock/reset":
                self._handle_mock_reset(body)
            elif path == "/api/mock/delete-order":
                self._handle_mock_delete_order(body)
            else:
                self._json_error("Not found", 404)
        except Exception as exc:
            self._json_error(str(exc), 500)

    # ── Handlers ─────────────────────────────────────────────────

    def _serve_dashboard(self) -> None:
        html_path = STATIC_DIR / "dashboard.html"
        if not html_path.exists():
            self._json_error("dashboard.html not found", 500)
            return
        content = html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_analyze(self, params: dict[str, str]) -> None:
        ticker = params.get("ticker", "NVDA").upper()
        market = validate_market(params.get("market", "US"))
        source = params.get("source", "demo")
        language = params.get("language", "ko")
        company = params.get("company") or None
        use_llm = params.get("llm", "1") not in {"0", "false", "no"}

        config = load_config(CONFIG_PATH)
        provider = make_provider(source, config.data_dir)
        analyzer = StrategyAnalyzer(config.min_score, config.min_rr)

        report = analyze_ticker(
            analyzer, provider, ticker, market, company, language, use_llm=use_llm
        )
        try:
            from alpha_bot.audit_log import log_query
            na = report.news_assessment
            log_query(
                ticker=ticker, market=market,
                signal=report.signal,
                score=report.scoreboard.total,
                rr=report.trade_plan.rr_ratio,
                reason=report.reason,
                source="web",
                news_sentiment=na.sentiment if na else None,
                news_adjustment=na.score_adjustment if na else None,
            )
        except Exception:
            pass
        data = _serialise(report)
        data["report_text"] = render_report(report)
        self._json_ok(data)

    def _handle_scan(self, params: dict[str, str]) -> None:
        watchlist = params.get("watchlist", "watchlist.example.yaml")
        source = params.get("source", "demo")
        language = params.get("language", "ko")

        config = load_config(CONFIG_PATH)
        provider = make_provider(source, config.data_dir)
        analyzer = StrategyAnalyzer(config.min_score, config.min_rr)
        rows = load_watchlist(Path(watchlist))

        results = []
        for row in rows:
            market = validate_market(row["market"])
            report = analyze_ticker(
                analyzer, provider, row["ticker"], market,
                row.get("company"), language, use_llm=False,
            )
            results.append({
                "ticker": report.ticker,
                "company_name": report.company_name,
                "market": report.market,
                "signal": report.signal,
                "score": report.scoreboard.total,
                "rr_ratio": round(report.trade_plan.rr_ratio, 2),
                "close": round(report.indicators.close, 2),
                "reason": report.reason,
            })
        self._json_ok(results)

    def _handle_exchange_rate(self) -> None:
        self._json_ok(_fetch_exchange_rate())

    def _handle_orders(self) -> None:
        config = load_config(CONFIG_PATH)
        orders = ApprovalQueue(config.approval_queue).list_orders()
        self._json_ok([_serialise(o) for o in orders])

    def _handle_approve(self, body: dict[str, str]) -> None:
        order_id = body.get("order_id", "")
        broker_name = body.get("broker", "mock")
        config = load_config(CONFIG_PATH)
        broker = make_broker(broker_name)
        updated, result = ApprovalQueue(config.approval_queue).approve(order_id, broker)
        self._json_ok({
            "order": _serialise(updated),
            "result": _serialise(result),
        })

    def _handle_queue(self, body: dict[str, Any]) -> None:
        """Re-analyze a ticker on the server and enqueue if eligible.

        Trade plan numbers are recomputed server-side rather than trusting the
        client, so the queued order always reflects the latest indicators.
        """
        ticker = str(body.get("ticker", "")).upper()
        market = validate_market(str(body.get("market", "US")))
        quantity = int(body.get("quantity", 1))
        source = str(body.get("source", "demo"))
        language = str(body.get("language", "ko"))
        use_llm = bool(body.get("use_llm", True))

        config = load_config(CONFIG_PATH)
        provider = make_provider(source, config.data_dir)
        analyzer = StrategyAnalyzer(config.min_score, config.min_rr)
        report = analyze_ticker(
            analyzer, provider, ticker, market, None, language, use_llm=use_llm
        )
        if report.signal not in {"Buy", "Strong Buy"}:
            self._json_error(
                f"신호가 {report.signal}여서 큐잉할 수 없습니다.", 400
            )
            return

        queue = ApprovalQueue(config.approval_queue)
        order = queue.enqueue(
            OrderRequest(
                ticker=report.ticker,
                market=report.market,
                side="buy",
                quantity=quantity,
                order_type="limit",
                limit_price=round(report.trade_plan.entry_high, 2),
                reason=report.reason,
            ),
            stop_loss=report.trade_plan.stop_loss,
            target1=report.trade_plan.target1,
            target2=report.trade_plan.target2,
            analysis_signal=report.signal,
        )
        self._json_ok({"order": _serialise(order)})

    def _handle_backtest(self, params: dict[str, str]) -> None:
        ticker = params.get("ticker", "NVDA").upper()
        market = validate_market(params.get("market", "US"))
        source = params.get("source", "demo")

        config = load_config(CONFIG_PATH)
        provider = make_provider(source, config.data_dir)
        candles = provider.get_candles(ticker, market, lookback=320)
        result = Backtester(StrategyAnalyzer(config.min_score, config.min_rr)).run(
            ticker, market, candles,
            provider.get_fundamentals(ticker, market),
            provider.get_catalysts(ticker, market),
            provider.get_market_context(ticker, market),
        )
        self._json_ok(_serialise(result))

    def _handle_config(self) -> None:
        config = load_config(CONFIG_PATH)
        self._json_ok(_serialise(config))

    def _handle_auto_start(self, body: dict[str, Any]) -> None:
        global auto_pilot_thread, auto_pilot_active, auto_pilot_started_at, auto_pilot_interval
        if auto_pilot_active:
            self._json_error("Auto-pilot is already running")
            return

        watchlist = body.get("watchlist", "watchlist.example.yaml")
        interval = int(body.get("interval", 300))
        broker = body.get("broker", "mock")
        quantity = int(body.get("quantity", 1))
        source = body.get("source", "kis")
        use_llm = bool(body.get("use_llm", True))
        cooldown_hours = int(body.get("cooldown_hours", 24))
        cooldown_enabled = bool(body.get("cooldown_enabled", True))
        auto_size = bool(body.get("auto_size", False))

        auto_pilot_active = True
        auto_pilot_started_at = time.strftime('%Y-%m-%d %H:%M:%S')
        auto_pilot_interval = interval
        auto_pilot_thread = threading.Thread(
            target=auto_pilot_loop,
            args=(watchlist, interval, broker, quantity, source, use_llm, cooldown_hours, cooldown_enabled, auto_size),
            daemon=True,
        )
        auto_pilot_thread.start()
        self._json_ok({"status": "started"})

    def _handle_auto_stop(self) -> None:
        global auto_pilot_active, auto_pilot_started_at, auto_pilot_interval
        auto_pilot_active = False
        auto_pilot_started_at = None
        auto_pilot_interval = 0
        self._json_ok({"status": "stopping"})

    def _handle_auto_status(self) -> None:
        global auto_pilot_active, auto_pilot_logs, auto_pilot_started_at, auto_pilot_interval
        self._json_ok({
            "active": auto_pilot_active,
            "logs": auto_pilot_logs,
            "started_at": auto_pilot_started_at,
            "interval": auto_pilot_interval,
        })

    def _handle_quotes(self, params: dict[str, str]) -> None:
        file_name = params.get("file", "watchlist.yaml")
        path = Path(file_name)
        if not path.exists():
            path = Path.cwd() / file_name
        tickers = load_watchlist(path) if path.exists() else []
        quotes = fetch_quotes(tickers)
        self._json_ok(quotes)

    def _handle_portfolio(self) -> None:
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
        self._json_ok({
            "open": open_positions,
            "closed": closed_trades,
            "brokers": brokers,
        })

    def _handle_bot_holdings(self, params: dict[str, str]) -> None:
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
            self._json_ok({"positions": [], "count": 0})
            return

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
        self._json_ok({"positions": result, "count": len(result)})

    def _handle_bot_stats(self) -> None:
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
        self._json_ok({"date": today, **stats})

    # ── Mock-Sim controls ───────────────────────────────────────────

    def _handle_mock_state(self) -> None:
        from alpha_bot.broker.mock import MockBroker
        broker = MockBroker()
        out: dict[str, Any] = {"markets": {}, "ledger": []}
        for market in ("KR", "US"):
            try:
                bal = broker.get_cash_balance(market)  # type: ignore[arg-type]
                pos = broker.get_positions(market)  # type: ignore[arg-type]
                out["markets"][market] = {
                    "starting_cash": broker.get_starting_cash(market),  # type: ignore[arg-type]
                    "cash": bal.cash,
                    "securities_value": bal.securities_value,
                    "total_value": bal.total_value,
                    "currency": bal.currency,
                    "positions": [
                        {
                            "ticker": p.ticker,
                            "quantity": p.quantity,
                            "avg_price": p.avg_price,
                            "market_value": p.market_value,
                        } for p in pos
                    ],
                }
            except Exception as exc:
                logger.warning("Mock state for %s failed: %s", market, exc)
                out["markets"][market] = {"error": str(exc)}
        out["ledger"] = list(reversed(broker.list_orders_raw()))  # newest first
        self._json_ok(out)

    def _handle_mock_order(self, body: dict[str, Any]) -> None:
        from alpha_bot.broker.mock import MockBroker
        ticker = str(body.get("ticker", "")).strip().upper()
        market = str(body.get("market", "")).upper()
        side = str(body.get("side", "")).lower()
        try:
            quantity = int(body.get("quantity", 0))
            price = float(body.get("price", 0))
        except (TypeError, ValueError):
            self._json_error("quantity/price must be numeric", 400)
            return
        if not ticker or market not in ("KR", "US"):
            self._json_error("ticker/market required (market ∈ KR|US)", 400)
            return
        if side not in ("buy", "sell"):
            self._json_error("side must be 'buy' or 'sell'", 400)
            return
        if quantity <= 0 or price <= 0:
            self._json_error("quantity and price must be positive", 400)
            return
        broker = MockBroker()
        try:
            result = broker.place_manual_order(ticker, market, side, quantity, price)
        except Exception as exc:
            self._json_error(str(exc), 500)
            return
        self._json_ok({
            "broker_order_id": result.broker_order_id,
            "accepted": result.accepted,
            "message": result.message,
        })

    def _handle_mock_set_cash(self, body: dict[str, Any]) -> None:
        from alpha_bot.broker.mock import MockBroker
        market = str(body.get("market", "")).upper()
        try:
            amount = float(body.get("amount", 0))
        except (TypeError, ValueError):
            self._json_error("amount must be numeric", 400)
            return
        if market not in ("KR", "US"):
            self._json_error("market must be KR or US", 400)
            return
        if amount < 0:
            self._json_error("amount must be non-negative", 400)
            return
        MockBroker().set_starting_cash(market, amount)  # type: ignore[arg-type]
        self._json_ok({"market": market, "starting_cash": amount})

    def _handle_mock_inject(self, body: dict[str, Any]) -> None:
        from alpha_bot.broker.mock import MockBroker
        ticker = str(body.get("ticker", "")).strip().upper()
        market = str(body.get("market", "")).upper()
        try:
            quantity = int(body.get("quantity", 0))
            avg_price = float(body.get("avg_price", 0))
        except (TypeError, ValueError):
            self._json_error("quantity/avg_price must be numeric", 400)
            return
        if not ticker or market not in ("KR", "US"):
            self._json_error("ticker/market required", 400)
            return
        if quantity <= 0 or avg_price <= 0:
            self._json_error("quantity and avg_price must be positive", 400)
            return
        broker = MockBroker()
        try:
            result = broker.inject_position(ticker, market, quantity, avg_price)  # type: ignore[arg-type]
        except Exception as exc:
            self._json_error(str(exc), 500)
            return
        self._json_ok({
            "broker_order_id": result.broker_order_id,
            "message": result.message,
        })

    def _handle_mock_reset(self, body: dict[str, Any]) -> None:
        from alpha_bot.broker.mock import MockBroker
        keep_cash = bool(body.get("keep_starting_cash", True))
        MockBroker().reset_state(keep_starting_cash=keep_cash)
        self._json_ok({"reset": True, "kept_starting_cash": keep_cash})

    def _handle_mock_delete_order(self, body: dict[str, Any]) -> None:
        from alpha_bot.broker.mock import MockBroker
        order_id = str(body.get("broker_order_id", "")).strip()
        if not order_id:
            self._json_error("broker_order_id required", 400)
            return
        removed = MockBroker().delete_order(order_id)
        if not removed:
            self._json_error("order not found", 404)
            return
        self._json_ok({"removed": order_id})

    def _handle_watchlist_load(self, params: dict[str, str]) -> None:
        file_name = params.get("file", "watchlist.yaml")
        path = Path(file_name)
        if not path.exists():
            path = Path.cwd() / file_name
        if not path.exists():
            self._json_ok({"file": file_name, "tickers": []})
            return
        rows = load_watchlist(path)
        self._json_ok({"file": file_name, "tickers": rows})

    def _handle_watchlist_save(self, body: dict[str, Any]) -> None:
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
        self._json_ok({"saved": file_name, "count": len(tickers)})

    def _handle_watchlists(self) -> None:
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
        self._json_ok(files)

    def _handle_settings(self) -> None:
        config = load_config(CONFIG_PATH)
        self._json_ok({
            "config": _serialise(config),
            "env": _read_env_safe(),
        })

    def _handle_sync_orders(self, body: dict[str, Any]) -> None:
        broker_name = str(body.get("broker", "kis"))
        broker = make_broker(broker_name)
        config = load_config(CONFIG_PATH)
        queue = ApprovalQueue(config.approval_queue)
        try:
            changed = queue.sync_with_broker(broker)
        except Exception as exc:
            self._json_error(f"동기화 실패: {exc}", 500)
            return
        self._json_ok({
            "broker": broker_name,
            "changed": [_serialise(o) for o in changed],
            "count": len(changed),
        })

    def _handle_account(self, params: dict[str, str]) -> None:
        broker_name = params.get("broker", "kis")
        market_param = params.get("market", "ALL").upper()
        broker = make_broker(broker_name)
        markets: list[str] = ["KR", "US"] if market_param == "ALL" else [market_param]
        out: dict[str, Any] = {"broker": broker_name, "markets": {}}
        for market in markets:
            try:
                balance = broker.get_cash_balance(market)  # type: ignore[arg-type]
                positions = broker.get_positions(market)  # type: ignore[arg-type]
                out["markets"][market] = {
                    "balance": _serialise(balance),
                    "positions": [_serialise(p) for p in positions],
                    "error": None,
                }
            except Exception as exc:
                logger.warning("Account query failed for %s/%s: %s", broker_name, market, exc)
                out["markets"][market] = {
                    "balance": None,
                    "positions": [],
                    "error": str(exc),
                }
        self._json_ok(out)

    def _handle_save_settings(self, body: dict[str, Any]) -> None:
        env = body.get("env") or {}
        if not isinstance(env, dict):
            self._json_error("env must be an object", 400)
            return
        # Only persist a known allowlist of keys; never write arbitrary input.
        allowed = {
            "KIS_MODE", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
            "KIS_ACCOUNT_PRODUCT", "KIS_HTS_ID", "BOT_BROKER",
            "BOT_APPROVAL_QUEUE", "OPENAI_API_KEY", "OPENAI_MODEL",
        }
        updates = {k: str(v) for k, v in env.items() if k in allowed}
        # Skip masked secret placeholders so we don't overwrite real values.
        updates = {k: v for k, v in updates.items() if "***" not in v}
        _write_env_partial(updates)
        self._json_ok({"saved": list(updates.keys())})

    # ── Response helpers ─────────────────────────────────────────

    def _json_ok(self, data: Any) -> None:
        body = json.dumps({"success": True, "data": data}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, message: str, code: int = 400) -> None:
        body = json.dumps({"success": False, "error": message}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Log / report helpers ─────────────────────────────────────────────

    def _handle_logs(self, params: dict[str, str]) -> None:
        from alpha_bot.audit_log import _LOG_DIR
        import json as _json

        date_str = params.get("date", "")
        event_type = params.get("type", "")    # query|queue|trade|cash_snapshot|llm_assessment
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

        # activity log
        if event_type != "llm_assessment":
            path = _LOG_DIR / f"activity_{date_str}.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if event_type and r.get("event") != event_type:
                        continue
                    if ticker and r.get("ticker", "").upper() != ticker:
                        continue
                    records.append(r)

        # LLM log
        if not event_type or event_type == "llm_assessment":
            path = _LOG_DIR / f"llm_{date_str}.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if ticker and r.get("ticker", "").upper() != ticker:
                        continue
                    records.append(r)

        # newest first, capped
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        from alpha_bot.daily_report import available_dates
        self._json_ok({
            "date": date_str,
            "total": len(records),
            "records": records[:limit],
            "available_dates": available_dates(),
        })

    def _handle_daily_report(self, params: dict[str, str]) -> None:
        from alpha_bot.daily_report import build_daily_summary
        date_str = params.get("date") or None
        self._json_ok(build_daily_summary(date_str))


# ── Exchange-rate helpers ─────────────────────────────────────────────

_RATE_CACHE: dict[str, Any] = {}   # {"rate": float, "ts": float}
_RATE_TTL = 3600.0  # 1 hour

def _fetch_exchange_rate() -> dict[str, Any]:
    """Return USD/KRW rate, cached for 1 hour. Fail-soft: returns None rate."""
    if _RATE_CACHE and time.time() - _RATE_CACHE.get("ts", 0) < _RATE_TTL:
        return _RATE_CACHE

    rate: float | None = None
    try:
        import yfinance as yf
        ticker = yf.Ticker("KRW=X")
        # fast_info is the lightest query
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


# ── Entry point ──────────────────────────────────────────────────────

def main() -> int:
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Alpha Strategy Bot Web Dashboard")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"\n  🚀 Alpha Strategy Bot Dashboard")
    print(f"  ➜ Local: {url}\n")
    logger.info("Serving on %s", url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
