from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto import (
    AutoTradeOptions,
    analyze_ticker,
    make_provider,
    run_auto_iteration,
)
from alpha_bot.backtest import Backtester
from alpha_bot.broker import KisBroker, MockBroker
from alpha_bot.config import load_config, load_watchlist
from alpha_bot.errors import BotError
from alpha_bot.models import OrderRequest
from alpha_bot.report import render_report
from alpha_bot.strategy import analyzer_from_config
from alpha_bot.utils import validate_market

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure root logger: INFO to stderr by default.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        return args.func(args)
    except (BotError, FileNotFoundError, ValueError) as exc:
        logger.error("Bot error: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bot", description="CANSLIM + VCP semi-automated quant bot")
    parser.add_argument("--config", default="config.yaml")
    subparsers = parser.add_subparsers(required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze one ticker")
    _add_analysis_args(analyze)
    analyze.add_argument("--queue-order", action="store_true", help="Create a pending buy order candidate if eligible")
    analyze.add_argument("--quantity", type=int, default=1)
    analyze.add_argument("--no-llm", action="store_true", help="Skip the LLM news assessment step")
    analyze.set_defaults(func=cmd_analyze)

    scan = subparsers.add_parser("scan", help="Analyze a watchlist")
    scan.add_argument("--universe", required=True)
    scan.add_argument("--data-dir")
    scan.add_argument("--demo", action="store_true")
    scan.add_argument("--language", default="ko")
    scan.set_defaults(func=cmd_scan)

    pending = subparsers.add_parser("pending", help="List pending/submitted orders")
    pending.add_argument("--queue")
    pending.set_defaults(func=cmd_pending)

    approve = subparsers.add_parser("approve", help="Approve and submit one queued order")
    approve.add_argument("--order-id", required=True)
    approve.add_argument("--broker", choices=["mock", "kis"])
    approve.add_argument("--queue")
    approve.set_defaults(func=cmd_approve)

    auto = subparsers.add_parser("auto", help="Run in fully automated mode (Auto-pilot)")
    auto.add_argument("--universe", required=True, help="Watchlist file to scan periodically")
    auto.add_argument("--interval", type=int, default=300, help="Scan interval in seconds (default: 300)")
    auto.add_argument("--broker", choices=["mock", "kis"])
    auto.add_argument("--quantity", type=int, default=1, help="Quantity to buy per trade")
    auto.add_argument("--data-dir")
    auto.add_argument("--demo", action="store_true")
    auto.add_argument("--kis-data", action="store_true")
    auto.add_argument("--language", default="ko")
    auto.add_argument("--cooldown-hours", type=int, default=24, help="Skip a ticker if it was already enqueued within this window")
    auto.add_argument("--no-llm", action="store_true", help="Skip the LLM news assessment step")
    auto.add_argument("--auto-size", action="store_true", help="Compute quantity from account balance and config.risk_per_trade_pct")
    auto.set_defaults(func=cmd_auto)

    backtest = subparsers.add_parser("backtest", help="Run a simple historical signal backtest")
    _add_analysis_args(backtest)
    backtest.set_defaults(func=cmd_backtest)

    return parser


def _add_analysis_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--company")
    parser.add_argument("--data-dir")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--kis-data", action="store_true", help="Use KIS REST for daily prices; fixtures fill fundamentals/news")
    parser.add_argument("--language", default="ko")


def cmd_analyze(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    provider = _provider(args, config.data_dir)
    market = validate_market(args.market)
    analyzer = analyzer_from_config(config)
    use_llm = not args.no_llm
    if use_llm:
        print("\n🔍 뉴스 수집 + LLM 평가 중... (몇 초 걸릴 수 있습니다)\n")
    report = analyze_ticker(
        analyzer,
        provider,
        args.ticker,
        market,
        args.company,
        args.language,
        use_llm=use_llm,
    )
    print(render_report(report))

    if args.queue_order and report.signal in {"Buy", "Strong Buy"}:
        queue = ApprovalQueue(config.approval_queue)
        order = queue.enqueue(
            OrderRequest(
                ticker=report.ticker,
                market=report.market,
                side="buy",
                quantity=args.quantity,
                order_type="limit",
                limit_price=round(report.trade_plan.entry_high, 2),
                reason=report.reason,
            ),
            stop_loss=report.trade_plan.stop_loss,
            target1=report.trade_plan.target1,
            target2=report.trade_plan.target2,
            analysis_signal=report.signal,
        )
        print(f"\nQueued pending order: {order.id}")
    elif args.queue_order:
        print(f"\nNo order queued because signal is {report.signal}.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    provider = _provider(args, config.data_dir)
    analyzer = analyzer_from_config(config)
    rows = load_watchlist(Path(args.universe))
    for row in rows:
        market = validate_market(row["market"])
        report = analyze_ticker(
            analyzer,
            provider,
            row["ticker"],
            market,
            row.get("company"),
            args.language,
            use_llm=False,
        )
        print(
            f"{report.market}:{report.ticker} {report.signal} "
            f"score={report.scoreboard.total}/30 rr={report.trade_plan.rr_ratio:.2f}:1 "
            f"close={report.indicators.close:.2f}"
        )
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    queue = ApprovalQueue(Path(args.queue) if args.queue else config.approval_queue)
    orders = queue.list_orders()
    if not orders:
        print("No queued orders.")
        return 0
    for order in orders:
        price = order.request.limit_price if order.request.limit_price is not None else "market"
        print(
            f"{order.id} {order.status} {order.request.market}:{order.request.ticker} "
            f"{order.request.side} qty={order.request.quantity} price={price} "
            f"signal={order.analysis_signal}"
        )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    broker_name = args.broker or config.broker
    broker = KisBroker() if broker_name == "kis" else MockBroker()
    queue = ApprovalQueue(Path(args.queue) if args.queue else config.approval_queue)
    order, result = queue.approve(args.order_id, broker)
    logger.info("Order %s approved via %s: %s", order.id, broker_name, result.message)
    print(f"{order.id} -> {order.status}: {result.message} ({result.broker_order_id})")
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    broker_name = args.broker or config.broker
    source = "demo" if args.demo else "kis" if args.kis_data else "local"
    opts = AutoTradeOptions(
        watchlist=Path(args.universe),
        broker_name=broker_name,
        quantity=args.quantity,
        source=source,
        language=args.language,
        cooldown_hours=args.cooldown_hours,
        use_llm=not args.no_llm,
        auto_size=args.auto_size,
    )

    print(
        f"🚀 Auto-pilot 시작 (broker={broker_name}, interval={args.interval}s, "
        f"llm={opts.use_llm}, auto_size={opts.auto_size})"
    )
    print(f"📄 Watchlist: {args.universe}")

    try:
        while True:
            print(f"\n--- 🔄 스캔 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
            run_auto_iteration(opts, config, log=lambda m: print(m))
            print(f"--- 💤 스캔 완료. {args.interval}s 대기 ---")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n🛑 Auto-pilot 종료 (사용자 중단)")
        return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    provider = _provider(args, config.data_dir)
    market = validate_market(args.market)
    candles = provider.get_candles(args.ticker, market, lookback=320)
    result = Backtester(analyzer_from_config(config)).run(
        args.ticker,
        market,
        candles,
        provider.get_fundamentals(args.ticker, market),
        provider.get_catalysts(args.ticker, market),
        provider.get_market_context(args.ticker, market),
    )
    print(
        f"{result.market}:{result.ticker} trades={len(result.trades)} "
        f"win_rate={result.win_rate:.1f}% total_return={result.total_return_pct:.1f}% "
        f"max_dd={result.max_drawdown_pct:.1f}% sharpe={result.sharpe_ratio:.2f}"
    )
    for trade in result.trades:
        print(
            f"{trade.entry_date}->{trade.exit_date} {trade.outcome} "
            f"entry={trade.entry:.2f} exit={trade.exit:.2f} return={trade.return_pct:.1f}%"
        )
    return 0


def _provider(args: argparse.Namespace, default_data_dir: Path):
    if args.demo:
        return make_provider("demo", default_data_dir)
    if args.kis_data:
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir
        return make_provider("kis", data_dir)
    return make_provider("local", Path(args.data_dir) if args.data_dir else default_data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
