"""Auto-trade iteration orchestrator.

Single source of truth for the "fetch news → LLM assess → score → enqueue
→ approve" flow used by both the CLI auto-pilot and the web dashboard.

Safety guards built in:
  * ``max_positions`` from config caps the number of pending+submitted orders.
  * Per-ticker cooldown prevents re-ordering the same name within N hours.
  * Failures on individual tickers never abort the iteration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto.analysis import analyze_ticker, make_broker, make_provider
from alpha_bot.auto.position_manager import (
    count_open_positions,
    find_held_buy,
    manage_open_positions,
    reconcile_queue_with_broker,
    should_force_exit,
    trigger_forced_exit,
)
from alpha_bot.auto.sizing import compute_position_size
from alpha_bot.config import AppConfig, load_watchlist
from alpha_bot.market_hours import market_status
from alpha_bot.market_regime import get_regime
from alpha_bot.models import OrderRequest
from alpha_bot.utils import validate_market

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoTradeOptions:
    watchlist: Path
    broker_name: str
    quantity: int
    source: str = "kis"
    language: str = "ko"
    cooldown_hours: int = 24
    cooldown_enabled: bool = True
    use_llm: bool = True
    auto_size: bool = False  # When True, quantity is computed from risk_per_trade_pct.


def run_auto_iteration(
    opts: AutoTradeOptions,
    config: AppConfig,
    log: Callable[[str], None] | None = None,
) -> None:
    """Run a single sweep over the watchlist.

    Caller is responsible for the outer scheduling loop (sleep, threading,
    KeyboardInterrupt handling).
    """

    say = log or (lambda msg: logger.info(msg))
    provider = make_provider(opts.source, config.data_dir)
    from alpha_bot.strategy import StrategyAnalyzer
    analyzer = StrategyAnalyzer(config.min_score, config.min_rr)
    queue = ApprovalQueue(config.approval_queue)
    broker = make_broker(opts.broker_name)

    try:
        changed = queue.sync_with_broker(broker)
        for order in changed:
            say(
                f"  🔁 {order.request.market}:{order.request.ticker} "
                f"{order.id} → {order.status} ({order.filled_quantity}/{order.request.quantity})"
            )
    except Exception as exc:
        logger.warning("Order sync failed: %s", exc)

    # ── Reconcile queue against actual broker positions ──
    # Catches cases where the user manually sold a bot-held name via the broker
    # UI: the broker shows 0 qty while our queue still records a "filled" buy
    # without an exit. Mark such entries as externally-closed so we don't try
    # to manage or re-buy them as if we still owned them.
    try:
        reconciled = reconcile_queue_with_broker(queue, broker)
        for o in reconciled:
            say(
                f"  🔄 {o.request.market}:{o.request.ticker} "
                f"외부 매도 감지 → 봇 보유 기록 정리"
            )
    except Exception as exc:
        logger.warning("Queue-broker reconciliation failed: %s", exc)

    try:
        manage_open_positions(queue, broker, provider, say)
    except Exception as exc:
        logger.exception("Position management failed")
        say(f"⚠️ 포지션 모니터링 실패: {exc}")

    open_count = count_open_positions(queue)
    if open_count >= config.max_positions:
        say(
            f"⚠️ 보유/대기 주문 {open_count}건이 max_positions({config.max_positions}) 도달, "
            "신규 매수 중단"
        )
        return

    rows = load_watchlist(opts.watchlist)
    cooldown = timedelta(hours=opts.cooldown_hours)

    market_cache: dict[str, bool] = {}
    regime_cache: dict[str, bool] = {}
    for row in rows:
        if open_count >= config.max_positions:
            say(f"⏸️ max_positions({config.max_positions}) 도달, 잔여 종목 스킵")
            break
        ticker = row["ticker"]
        try:
            market = validate_market(row["market"])
        except ValueError as exc:
            say(f"⚠️ {ticker}: {exc}")
            continue

        if market not in market_cache:
            status = market_status(market)
            market_cache[market] = status.is_open
            if not status.is_open:
                say(f"🌙 {market} 시장 {status.reason} — 신규 매수 스킵")
        if not market_cache[market]:
            continue

        # ── CANSLIM "M" filter: block new entries in a bearish broad index ──
        if market not in regime_cache:
            regime = get_regime(market)
            regime_cache[market] = regime.is_bullish
            if not regime.is_bullish:
                say(f"🐻 {market} 레짐 약세: {regime.reason}")
        if not regime_cache[market]:
            continue

        try:
            if opts.cooldown_enabled and _on_cooldown(queue, ticker, market, cooldown):
                say(f"[{market}:{ticker}] ⏳ 쿨다운({opts.cooldown_hours}h) 중, 스킵")
                continue

            report = analyze_ticker(
                analyzer,
                provider,
                ticker,
                market,
                row.get("company"),
                opts.language,
                opts.use_llm,
            )
            news_msg = ""
            if report.news_assessment:
                na = report.news_assessment
                news_msg = f" / 뉴스 {na.sentiment}({na.score_adjustment:+d}, {na.severity})"
            say(
                f"[{market}:{ticker}] {report.signal} "
                f"score={report.scoreboard.total}/30 "
                f"rr={report.trade_plan.rr_ratio:.2f}{news_msg}"
            )

            held = find_held_buy(queue, market, ticker)
            if held is not None:
                force_exit, detail = should_force_exit(report)
                if force_exit:
                    trigger_forced_exit(
                        queue, broker, held, "악재 청산", detail, say
                    )
                continue  # Already in position — skip new entry regardless of signal

            if report.signal not in {"Buy", "Strong Buy"}:
                continue

            entry_price = round(report.trade_plan.entry_high, 2)
            quantity = opts.quantity
            if opts.auto_size:
                quantity, note = compute_position_size(
                    broker,
                    report.market,
                    entry_price,
                    report.trade_plan.stop_loss,
                    config.risk_per_trade_pct,
                )
                say(f"  📐 {ticker} 사이징: {quantity}주 — {note}")
                if quantity <= 0:
                    say(f"  ⏭️ {ticker} 자동 사이징 0주, 스킵")
                    continue

            # ── Pre-flight: verify the broker actually has the cash. ──
            # The auto-sizing path already caps qty by available cash, but the
            # fixed-quantity path doesn't, and account state can drift between
            # iterations (manual buys, FX moves, prior fills). Block here so
            # we never submit an order the broker will reject for insufficient
            # funds — those rejections clog the queue with stale "rejected"
            # rows and trigger spurious alerts.
            estimated_cost = entry_price * quantity * 1.005  # 0.5% headroom
            try:
                bal = broker.get_cash_balance(market) if hasattr(broker, "get_cash_balance") else None
            except Exception as exc:
                logger.debug("Pre-flight balance query failed for %s: %s", market, exc)
                bal = None
            if bal is not None:
                available = bal.cash
                if available <= 0 and bal.total_value > bal.securities_value:
                    available = max(0.0, bal.total_value - bal.securities_value)
                if estimated_cost > available:
                    say(
                        f"  💸 {ticker} 현금 부족: 필요 ~{estimated_cost:.0f}{bal.currency}, "
                        f"가용 {available:.0f}{bal.currency} — 매수 건너뜀"
                    )
                    continue

            say(f"  ➡️ {ticker} 매수 신호 → {opts.broker_name} 주문 생성 ({quantity}주)")
            order = queue.enqueue(
                OrderRequest(
                    ticker=report.ticker,
                    market=report.market,
                    side="buy",
                    quantity=quantity,
                    order_type="limit",
                    limit_price=entry_price,
                    reason=report.reason,
                ),
                stop_loss=report.trade_plan.stop_loss,
                target1=report.trade_plan.target1,
                target2=report.trade_plan.target2,
                analysis_signal=report.signal,
            )
            try:
                approved, result = queue.approve(order.id, broker)
            except Exception as exc:
                say(f"  ❌ {ticker} 주문 실패: {exc}")
                continue

            if approved.status == "submitted":
                open_count += 1
                say(
                    f"  ✅ {ticker} 주문 성공 (id={approved.id}, "
                    f"broker_ref={result.broker_order_id})"
                )
                # 거래 직후 예수금 스냅샷 기록
                try:
                    from alpha_bot.audit_log import log_cash_snapshot
                    bal = broker.get_cash_balance(market)
                    log_cash_snapshot(
                        market=market,
                        broker=broker.name,
                        cash=bal.cash,
                        currency=bal.currency,
                        total_value=bal.total_value,
                        trigger=f"after_buy:{ticker}",
                    )
                    say(
                        f"  💰 예수금 스냅샷: {bal.currency} {bal.cash:,.0f} "
                        f"(총자산 {bal.total_value:,.0f})"
                    )
                except Exception as _exc:
                    logger.debug("Cash snapshot skipped: %s", _exc)
            else:
                say(f"  ❌ {ticker} 주문 거부: {result.message}")

        except Exception as exc:
            logger.exception("Error processing %s", ticker)
            say(f"  ⚠️ {ticker} 처리 중 오류: {exc}")


def _on_cooldown(
    queue: ApprovalQueue, ticker: str, market: str, cooldown: timedelta
) -> bool:
    if cooldown.total_seconds() <= 0:
        return False
    cutoff = datetime.now(timezone.utc) - cooldown
    for order in queue.list_orders():
        if (
            order.request.ticker == ticker
            and order.request.market == market
            and order.request.side == "buy"
        ):
            try:
                created = datetime.fromisoformat(
                    order.created_at.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= cutoff:
                return True
    return False
