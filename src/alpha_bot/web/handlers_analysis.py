"""Analysis, scan, and backtest web API handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto import analyze_ticker, make_provider
from alpha_bot.backtest import Backtester
from alpha_bot.config import load_config, load_watchlist
from alpha_bot.models import OrderRequest
from alpha_bot.report import render_report
from alpha_bot.strategy import analyzer_from_config
from alpha_bot.utils import validate_market

CONFIG_PATH = Path("config.yaml")


def handle_analyze(params: dict[str, str], serialise: Any) -> dict[str, Any]:
    ticker = params.get("ticker", "NVDA").upper()
    market = validate_market(params.get("market", "US"))
    source = params.get("source", "demo")
    language = params.get("language", "ko")
    company = params.get("company") or None
    use_llm = params.get("llm", "1") not in {"0", "false", "no"}

    config = load_config(CONFIG_PATH)
    provider = make_provider(source, config.data_dir)
    analyzer = analyzer_from_config(config)

    report = analyze_ticker(
        analyzer, provider, ticker, market, company, language, use_llm=use_llm
    )
    data = serialise(report)
    data["report_text"] = render_report(report)
    return data


def handle_scan(params: dict[str, str], serialise: Any) -> list[dict[str, Any]]:
    watchlist = params.get("watchlist", "watchlist.example.yaml")
    source = params.get("source", "demo")
    language = params.get("language", "ko")

    config = load_config(CONFIG_PATH)
    provider = make_provider(source, config.data_dir)
    analyzer = analyzer_from_config(config)
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
    return results


def handle_backtest(params: dict[str, str], serialise: Any) -> Any:
    ticker = params.get("ticker", "NVDA").upper()
    market = validate_market(params.get("market", "US"))
    source = params.get("source", "demo")

    config = load_config(CONFIG_PATH)
    provider = make_provider(source, config.data_dir)
    candles = provider.get_candles(ticker, market, lookback=320)
    result = Backtester(analyzer_from_config(config)).run(
        ticker, market, candles,
        provider.get_fundamentals(ticker, market),
        provider.get_catalysts(ticker, market),
        provider.get_market_context(ticker, market),
    )
    return serialise(result)


def handle_queue(body: dict[str, Any], serialise: Any) -> dict[str, Any] | tuple[str, int]:
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
    analyzer = analyzer_from_config(config)
    report = analyze_ticker(
        analyzer, provider, ticker, market, None, language, use_llm=use_llm
    )
    if report.signal not in {"Buy", "Strong Buy"}:
        return (f"신호가 {report.signal}여서 큐잉할 수 없습니다.", 400)

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
    return {"order": serialise(order)}
