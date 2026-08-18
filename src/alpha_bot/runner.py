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
    make_broker,
    make_provider,
    run_auto_iteration,
)
from alpha_bot.backtest import Backtester
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
    scan.add_argument("--kis-data", action="store_true", help="Use KIS REST for daily prices")
    scan.add_argument("--toss-data", action="store_true", help="Use Toss REST for daily prices")
    scan.add_argument("--language", default="ko")
    scan.set_defaults(func=cmd_scan)

    pending = subparsers.add_parser("pending", help="List pending/submitted orders")
    pending.add_argument("--queue")
    pending.set_defaults(func=cmd_pending)

    approve = subparsers.add_parser("approve", help="Approve and submit one queued order")
    approve.add_argument("--order-id", required=True)
    approve.add_argument("--broker", choices=["mock", "kis", "toss"])
    approve.add_argument("--queue")
    approve.set_defaults(func=cmd_approve)

    auto = subparsers.add_parser("auto", help="Run in fully automated mode (Auto-pilot)")
    auto.add_argument("--universe", required=True, help="Watchlist file to scan periodically")
    auto.add_argument("--interval", type=int, default=300, help="Scan interval in seconds (default: 300)")
    auto.add_argument("--broker", choices=["mock", "kis", "toss"])
    auto.add_argument("--quantity", type=int, default=1, help="Quantity to buy per trade")
    auto.add_argument("--data-dir")
    auto.add_argument("--demo", action="store_true")
    auto.add_argument("--kis-data", action="store_true")
    auto.add_argument("--toss-data", action="store_true")
    auto.add_argument("--language", default="ko")
    auto.add_argument("--cooldown-hours", type=int, default=24, help="Skip a ticker if it was already enqueued within this window")
    auto.add_argument("--no-llm", action="store_true", help="Skip the LLM news assessment step")
    auto.add_argument("--auto-size", action="store_true", help="Compute quantity from account balance and config.risk_per_trade_pct")
    auto.set_defaults(func=cmd_auto)

    monitor = subparsers.add_parser(
        "monitor",
        help="Exit monitor — KR streams when available, with KR/US REST fallback",
    )
    monitor.add_argument("--broker", choices=["mock", "kis", "toss"])
    monitor.add_argument("--data-dir")
    monitor.add_argument("--demo", action="store_true")
    monitor.add_argument("--kis-data", action="store_true", help="Use KIS REST for the daily candles (ATR/trail math)")
    monitor.add_argument("--toss-data", action="store_true", help="Use Toss REST for daily candles")
    monitor.add_argument("--eval-interval", type=float, default=2.0, help="Seconds between exit passes when fresh ticks arrived")
    monitor.add_argument("--resub-interval", type=float, default=30.0, help="Seconds between position→subscription syncs")
    monitor.add_argument("--rest-poll-interval", type=float, default=15.0, help="KR/US REST fallback seconds (default: 15)")
    monitor.set_defaults(func=cmd_monitor)

    watchdog = subparsers.add_parser(
        "watchdog",
        help="Alert when the auto-pilot or exit monitor heartbeat becomes stale",
    )
    watchdog.add_argument(
        "--component", action="append", choices=["auto", "monitor"],
        help="Component to require; repeat for both (default: both)",
    )
    watchdog.add_argument("--auto-timeout", type=float, default=420.0)
    watchdog.add_argument("--monitor-timeout", type=float, default=60.0)
    watchdog.add_argument("--interval", type=float, default=15.0)
    watchdog.add_argument("--startup-grace", type=float, default=30.0)
    watchdog.add_argument("--heartbeat-dir")
    watchdog.add_argument("--once", action="store_true", help="Check once and exit")
    watchdog.set_defaults(func=cmd_watchdog)

    backtest = subparsers.add_parser(
        "backtest",
        help="Historical signal backtest — single ticker, or a whole watchlist as a portfolio",
    )
    backtest.add_argument("--ticker", help="Single-ticker mode (requires --market)")
    backtest.add_argument("--market", choices=["KR", "US"])
    backtest.add_argument("--company")
    backtest.add_argument("--data-dir")
    backtest.add_argument("--demo", action="store_true")
    backtest.add_argument("--kis-data", action="store_true", help="Use KIS REST for daily prices; fixtures fill fundamentals/news")
    backtest.add_argument("--toss-data", action="store_true", help="Use Toss REST for adjusted daily prices")
    backtest.add_argument("--language", default="ko")
    backtest.add_argument(
        "--universe",
        help="Watchlist file → portfolio mode: shared cash, max_positions, risk sizing (one run per market)",
    )
    backtest.add_argument(
        "--cash", type=float,
        help="Starting cash per market portfolio (default: KR 10,000,000 / US 10,000)",
    )
    backtest.set_defaults(func=cmd_backtest)

    return parser


def _add_analysis_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--company")
    parser.add_argument("--data-dir")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--kis-data", action="store_true", help="Use KIS REST for daily prices; fixtures fill fundamentals/news")
    parser.add_argument("--toss-data", action="store_true", help="Use Toss REST for adjusted daily prices")
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
        queue = ApprovalQueue(config.approval_queue, protected_tickers=config.protected_tickers)
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
    queue = ApprovalQueue(
        Path(args.queue) if args.queue else config.approval_queue,
        protected_tickers=config.protected_tickers,
    )
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
    broker = make_broker(broker_name)
    queue = ApprovalQueue(
        Path(args.queue) if args.queue else config.approval_queue,
        protected_tickers=config.protected_tickers,
    )
    order, result = queue.approve(args.order_id, broker)
    logger.info("Order %s approved via %s: %s", order.id, broker_name, result.message)
    print(f"{order.id} -> {order.status}: {result.message} ({result.broker_order_id})")
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    if args.kis_data and args.toss_data:
        raise ValueError("Choose only one market-data source: --kis-data or --toss-data.")
    broker_name = args.broker or config.broker
    source = (
        "demo" if args.demo else "toss" if args.toss_data
        else "kis" if args.kis_data else "local"
    )
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

    from alpha_bot.auto.watchdog import sleep_with_heartbeat, write_heartbeat

    write_heartbeat(
        "auto", status="starting", detail={"broker": broker_name}
    )
    try:
        while True:
            write_heartbeat(
                "auto", status="scanning", detail={"broker": broker_name}
            )
            print(f"\n--- 🔄 스캔 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
            run_auto_iteration(opts, config, log=lambda m: print(m))
            write_heartbeat(
                "auto", status="idle", detail={"broker": broker_name}
            )
            print(f"--- 💤 스캔 완료. {args.interval}s 대기 ---")
            sleep_with_heartbeat(args.interval, "auto")
    except KeyboardInterrupt:
        write_heartbeat("auto", status="stopped", detail={"broker": broker_name})
        print("\n🛑 Auto-pilot 종료 (사용자 중단)")
        return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    broker_name = args.broker or config.broker
    if (
        broker_name == "toss"
        and not args.demo
        and not args.kis_data
        and not args.toss_data
    ):
        provider = make_provider("toss", config.data_dir)
    else:
        provider = _provider(args, config.data_dir)
    queue = ApprovalQueue(config.approval_queue, protected_tickers=config.protected_tickers)
    broker = make_broker(broker_name)

    from alpha_bot.auto.live_monitor import LiveExitMonitor
    monitor = LiveExitMonitor(
        queue, broker, provider, say=print,
        protective_stops=config.protective_stop,
        protected_tickers=config.protected_tickers,
    )
    stream = None
    watch: set[str] = set()
    if broker_name == "kis":
        from alpha_bot.data.stream import KisStreamClient
        stream = KisStreamClient(
            on_tick=monitor.on_tick, on_status=lambda m: print(f"  📡 {m}")
        )
        watch = monitor.sync_subscriptions(stream)
    print(
        f"👁️ 청산 모니터 시작 (broker={broker_name}, "
        f"KR 실시간 {len(watch)}종목, REST 보조 {args.rest_poll_interval:g}초)"
    )
    if stream is not None:
        stream.start()
    last_resub = 0.0
    from alpha_bot.auto.watchdog import write_heartbeat
    write_heartbeat("monitor", broker=broker, status="starting")
    try:
        while True:
            now = time.monotonic()
            if stream is not None and now - last_resub >= args.resub_interval:
                monitor.sync_subscriptions(stream)
                last_resub = now
            evaluated = monitor.evaluate_if_due(args.rest_poll_interval)
            write_heartbeat(
                "monitor", broker=broker, status="running",
                detail={"evaluated": evaluated},
            )
            time.sleep(args.eval_interval)
    except KeyboardInterrupt:
        write_heartbeat("monitor", broker=broker, status="stopped")
        print("\n🛑 모니터 종료 (사용자 중단)")
        return 0
    finally:
        if stream is not None:
            stream.stop()


def cmd_watchdog(args: argparse.Namespace) -> int:
    from alpha_bot.auto.watchdog import check_heartbeat
    from alpha_bot.notify import notify

    components = list(dict.fromkeys(args.component or ["auto", "monitor"]))
    directory = Path(args.heartbeat_dir) if args.heartbeat_dir else None
    timeouts = {"auto": args.auto_timeout, "monitor": args.monitor_timeout}
    if args.interval <= 0 or args.startup_grace < 0:
        raise ValueError("watchdog interval must be positive and grace non-negative")
    for component in components:
        if timeouts[component] <= 0:
            raise ValueError(f"{component} timeout must be positive")

    print(f"🐕 Watchdog 시작 (감시: {', '.join(components)})")
    started = time.monotonic()
    previous: dict[str, bool] = {}
    while True:
        unhealthy = False
        in_grace = time.monotonic() - started < args.startup_grace
        for component in components:
            health = check_heartbeat(
                component, timeouts[component], directory=directory
            )
            if not health.healthy and in_grace and health.record is None:
                continue
            # Paging someone because the bot was switched off on purpose is
            # how alerts get muted, and a muted alert is worse than none.
            # Absence still pages after the grace window: a component that
            # was asked for and never appeared is what this exists to catch.
            failing = health.alarming or health.state == "absent"
            unhealthy = unhealthy or failing
            prior = previous.get(component)
            if health.stopped_deliberately and prior is not True:
                print(f"⏹️  AlphaBot {component} 중지됨 (정상 종료)")
            if failing and prior is not False:
                message = f"🚨 AlphaBot {component} 생존 신호 이상\n{health.reason}"
                print(message)
                notify(
                    message,
                    dedupe_key=f"watchdog:{component}:unhealthy",
                    dedupe_ttl=max(int(timeouts[component]), 60),
                )
            elif health.healthy and prior is False:
                message = (
                    f"✅ AlphaBot {component} 생존 신호 복구 "
                    f"(지연 {health.age_seconds or 0:.0f}초)"
                )
                print(message)
                notify(message, dedupe_key=f"watchdog:{component}:recovered", dedupe_ttl=60)
            previous[component] = not failing

        if args.once:
            return 1 if unhealthy else 0
        time.sleep(args.interval)


def cmd_backtest(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    provider = _provider(args, config.data_dir)
    if args.universe:
        return _portfolio_backtest(args, config, provider)
    if not args.ticker or not args.market:
        raise ValueError("backtest needs --ticker and --market, or --universe for portfolio mode")
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
    for limitation in result.limitations:
        print(f"⚠️ 백테스트 한계: {limitation}")
    for trade in result.trades:
        print(
            f"{trade.entry_date}->{trade.exit_date} {trade.outcome} "
            f"entry={trade.entry:.2f} exit={trade.exit:.2f} return={trade.return_pct:.1f}%"
        )
    return 0


_DEFAULT_PORTFOLIO_CASH = {"KR": 10_000_000.0, "US": 10_000.0}


def _portfolio_backtest(args: argparse.Namespace, config, provider) -> int:
    from alpha_bot.portfolio_backtest import PortfolioBacktester, TickerSeries

    rows = load_watchlist(Path(args.universe))
    groups: dict[str, list[TickerSeries]] = {}
    for row in rows:
        market = validate_market(row["market"])
        try:
            groups.setdefault(market, []).append(
                TickerSeries(
                    ticker=row["ticker"],
                    market=market,
                    candles=provider.get_candles(row["ticker"], market, lookback=320),
                    fundamentals=provider.get_fundamentals(row["ticker"], market),
                    catalysts=provider.get_catalysts(row["ticker"], market),
                    context=provider.get_market_context(row["ticker"], market),
                )
            )
        except (BotError, FileNotFoundError) as exc:
            print(f"⚠️ {market}:{row['ticker']} 데이터 없음, 스킵: {exc}")
    if not groups:
        raise ValueError("No usable tickers in the universe (missing price data?).")

    for market, universe in sorted(groups.items()):
        engine = PortfolioBacktester(
            analyzer_from_config(config),
            starting_cash=args.cash or _DEFAULT_PORTFOLIO_CASH.get(market, 10_000.0),
            max_positions=config.max_positions,
            risk_per_trade_pct=config.risk_per_trade_pct,
            max_position_pct=config.max_position_pct,
        )
        result = engine.run(universe)
        currency = "KRW" if market == "KR" else "USD"
        print(
            f"\n[{market}] {len(universe)}종목 · 시작 {result.starting_cash:,.0f}{currency} "
            f"→ 종료 {result.ending_equity:,.0f}{currency} ({result.total_return_pct:+.2f}%)"
        )
        print(
            f"  trades={len(result.trades)} win_rate={result.win_rate:.1f}% "
            f"max_dd={result.max_drawdown_pct:.2f}% sharpe={result.sharpe_ratio:.2f} "
            f"max_concurrent={result.max_concurrent_positions} skipped={result.skipped_entries}"
        )
        for limitation in result.limitations:
            print(f"  ⚠️ 백테스트 한계: {limitation}")
        for t in result.trades:
            print(
                f"  {t.entry_date}->{t.exit_date} {t.ticker:8s} {t.outcome:18s} "
                f"qty={t.shares} entry={t.entry:,.2f} exit={t.exit:,.2f} "
                f"ret={t.return_pct:+.2f}% pnl={t.pnl:+,.0f}"
            )
    return 0


def _provider(args: argparse.Namespace, default_data_dir: Path):
    # Not every subcommand defines the source flags; treat absence as False
    # instead of crashing (bot scan shipped without them and broke outright).
    for flag in ("kis_data", "toss_data", "demo"):
        if not hasattr(args, flag):
            setattr(args, flag, False)
    if args.kis_data and args.toss_data:
        raise ValueError("Choose only one market-data source: --kis-data or --toss-data.")
    if args.demo:
        return make_provider("demo", default_data_dir)
    if args.kis_data:
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir
        return make_provider("kis", data_dir)
    if args.toss_data:
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir
        return make_provider("toss", data_dir)
    return make_provider("local", Path(args.data_dir) if args.data_dir else default_data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
