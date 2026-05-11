"""Web dashboard server for Alpha Strategy Bot.

Run with:
    python3 -m alpha_bot.web          # http://localhost:8501
    python3 -m alpha_bot.web --port 9000

This module contains only the HTTP handler (routing + response helpers)
and the ``main()`` entry point. All business logic lives in the
``handlers_*`` sub-modules.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.parse
from dataclasses import fields, is_dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpha_bot.web import handlers_analysis as _analysis
from alpha_bot.web import handlers_config as _config
from alpha_bot.web import handlers_mock as _mock
from alpha_bot.web import handlers_orders as _orders
from alpha_bot.web import handlers_portfolio as _portfolio

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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
    """Serves the web dashboard SPA and JSON API."""

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)

    # ── GET routes ───────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = dict(urllib.parse.parse_qsl(parsed.query))

        routes: dict[str, Any] = {
            "/": self._serve_dashboard,
            "/api/analyze": lambda: self._dispatch(_analysis.handle_analyze(params, _serialise)),
            "/api/scan": lambda: self._dispatch(_analysis.handle_scan(params, _serialise)),
            "/api/orders": lambda: self._dispatch(_orders.handle_orders(_serialise)),
            "/api/backtest": lambda: self._dispatch(_analysis.handle_backtest(params, _serialise)),
            "/api/config": lambda: self._dispatch(_config.handle_config(_serialise)),
            "/api/auto/status": lambda: self._dispatch(_config.handle_auto_status()),
            "/api/watchlists": lambda: self._dispatch(_config.handle_watchlists()),
            "/api/watchlist/load": lambda: self._dispatch(_config.handle_watchlist_load(params)),
            "/api/quotes": lambda: self._dispatch(_config.handle_quotes(params)),
            "/api/portfolio": lambda: self._dispatch(_portfolio.handle_portfolio(_serialise)),
            "/api/settings": lambda: self._dispatch(_config.handle_settings(_serialise)),
            "/api/account": lambda: self._dispatch(_config.handle_account(params, _serialise)),
            "/api/exchange-rate": lambda: self._dispatch(_config.handle_exchange_rate()),
            "/api/logs": lambda: self._dispatch(_config.handle_logs(params)),
            "/api/daily-report": lambda: self._dispatch(_config.handle_daily_report(params)),
            "/api/bot/holdings": lambda: self._dispatch(_portfolio.handle_bot_holdings(params)),
            "/api/bot/stats": lambda: self._dispatch(_portfolio.handle_bot_stats()),
            "/api/mock/state": lambda: self._dispatch(_mock.handle_mock_state()),
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

            post_routes: dict[str, Any] = {
                "/api/orders/approve": lambda: self._dispatch(_orders.handle_approve(body, _serialise)),
                "/api/orders/queue": lambda: self._dispatch(_analysis.handle_queue(body, _serialise)),
                "/api/orders/sync": lambda: self._dispatch(_orders.handle_sync_orders(body, _serialise)),
                "/api/auto/start": lambda: self._dispatch(_config.handle_auto_start(body)),
                "/api/auto/stop": lambda: self._dispatch(_config.handle_auto_stop()),
                "/api/settings": lambda: self._dispatch(_config.handle_save_settings(body)),
                "/api/watchlist/save": lambda: self._dispatch(_config.handle_watchlist_save(body)),
                "/api/mock/order": lambda: self._dispatch(_mock.handle_mock_order(body)),
                "/api/mock/set-cash": lambda: self._dispatch(_mock.handle_mock_set_cash(body)),
                "/api/mock/inject": lambda: self._dispatch(_mock.handle_mock_inject(body)),
                "/api/mock/reset": lambda: self._dispatch(_mock.handle_mock_reset(body)),
                "/api/mock/delete-order": lambda: self._dispatch(_mock.handle_mock_delete_order(body)),
            }
            handler = post_routes.get(path)
            if handler:
                handler()
            else:
                self._json_error("Not found", 404)
        except Exception as exc:
            self._json_error(str(exc), 500)

    # ── Dashboard ────────────────────────────────────────────────

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

    # ── Dispatch helper ──────────────────────────────────────────

    def _dispatch(self, result: Any) -> None:
        """Convert handler return values to HTTP responses.

        Handlers return either:
          * A dict/list → 200 OK with JSON body
          * A tuple (message, status_code) → error response
        """
        if isinstance(result, tuple) and len(result) == 2:
            msg, code = result
            self._json_error(str(msg), int(code))
        else:
            self._json_ok(result)

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
