"""Unified analyze + auto-trade pipeline.

Single source of truth for the "fetch news → LLM assess → score → enqueue
→ approve" flow used by both the CLI auto-pilot and the web dashboard.

Safety guards built in:
  * `max_positions` from config caps the number of pending+submitted orders.
  * Per-ticker cooldown prevents re-ordering the same name within N hours.
  * Failures on individual tickers never abort the iteration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from dataclasses import replace as _replace

from alpha_bot.approval import ApprovalQueue
from alpha_bot.broker import KisBroker, MockBroker
from alpha_bot.broker.base import Broker
from alpha_bot.config import AppConfig, load_watchlist
from alpha_bot.data import (
    DataProvider,
    FixtureDataProvider,
    KisPriceDataProvider,
    SyntheticDataProvider,
)
from alpha_bot.models import (
    AnalysisReport,
    Candle,
    Market,
    NewsAssessment,
    OrderCandidate,
    OrderRequest,
)
from alpha_bot.market_hours import market_status
from alpha_bot.market_regime import get_regime
from alpha_bot.news import assess_news, neutral_assessment
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.utils import validate_market

logger = logging.getLogger(__name__)


# ── Provider / broker factories ──────────────────────────────────────


def make_provider(source: str, data_dir: Path) -> DataProvider:
    if source == "demo":
        return SyntheticDataProvider()
    if source == "kis":
        return KisPriceDataProvider(FixtureDataProvider(data_dir))
    return FixtureDataProvider(data_dir)


def make_broker(name: str) -> Broker:
    return KisBroker() if name == "kis" else MockBroker()


# ── One-shot analysis with optional LLM news ─────────────────────────


def analyze_ticker(
    analyzer: StrategyAnalyzer,
    provider: DataProvider,
    ticker: str,
    market: Market,
    company: str | None = None,
    language: str = "ko",
    use_llm: bool = True,
) -> AnalysisReport:
    candles = provider.get_candles(ticker, market)
    fundamentals = provider.get_fundamentals(ticker, market)
    catalysts = provider.get_catalysts(ticker, market)
    context = provider.get_market_context(ticker, market)

    assessment: NewsAssessment | None = None
    if use_llm:
        assessment = _gather_news_assessment(ticker, market, catalysts)

    return analyzer.analyze(
        ticker,
        market,
        candles,
        fundamentals,
        catalysts,
        context,
        company_name=company,
        language=language,
        news_assessment=assessment,
    )


def _gather_news_assessment(ticker: str, market: Market, catalysts) -> NewsAssessment:
    try:
        from alpha_bot.data.scraper import fetch_news
    except ImportError as exc:
        logger.warning("News scraper unavailable (%s); skipping LLM assessment", exc)
        return neutral_assessment("뉴스 수집 모듈 의존성 없음", "")
    try:
        news_text = fetch_news(ticker, market)
    except Exception as exc:
        logger.warning("News fetch failed for %s: %s", ticker, exc)
        return neutral_assessment(f"뉴스 수집 실패: {exc}", "")
    return assess_news(ticker, market, news_text, catalysts)


# ── Auto-trading iteration ───────────────────────────────────────────


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

    open_count = _count_open_positions(queue)
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

            held = _find_held_buy(queue, market, ticker)
            if held is not None:
                force_exit, detail = should_force_exit(report)
                if force_exit:
                    _trigger_forced_exit(
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


_OPEN_STATUSES = {"pending", "submitted", "partially_filled", "filled"}


def _count_open_positions(queue: ApprovalQueue) -> int:
    """Pending intent + still-working + filled (and not yet fully exited)
    count toward the max_positions cap. Cancelled/rejected and fully-exited
    buys do not."""
    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    seen: set[tuple[str, str]] = set()
    count = 0
    for order in orders:
        if order.request.side != "buy":
            continue
        if order.status not in _OPEN_STATUSES:
            continue
        exit_order = by_id.get(order.exit_order_id or "")
        if exit_order and exit_order.status == "filled":
            continue  # Already exited.
        key = (order.request.market, order.request.ticker)
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


# ── Force-exit signal extraction ─────────────────────────────────────


def should_force_exit(report: AnalysisReport) -> tuple[bool, str]:
    """Return ``(True, reason)`` if the analysis indicates we should exit
    a held position regardless of price levels.

    Triggers:
      * LLM detected severe negative news (severity=high, sentiment=negative)
      * earnings_caution flag from LLM or local fundamentals (EPS/매출 YoY < 0)
    """

    na = report.news_assessment
    if na and na.severity == "high" and na.sentiment == "negative":
        return True, f"심각한 악재(LLM): {na.reasoning or '뉴스 톤 매우 부정적'}"
    if report.earnings_caution:
        return True, "earnings_caution 플래그 (실적 둔화/하향 또는 LLM 경고)"
    return False, ""


def _find_held_buy(queue: ApprovalQueue, market: Market, ticker: str) -> OrderCandidate | None:
    """Return any active buy order for the ticker — filled, partially filled,
    or still in-flight (submitted/pending). This prevents the auto-pilot from
    opening a second position while the first order is still working."""
    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    for buy in orders:
        if buy.request.side != "buy":
            continue
        if buy.request.market != market or buy.request.ticker != ticker:
            continue
        # In-flight buy (not yet confirmed by broker): treat as held to block re-entry.
        if buy.status in {"pending", "submitted"}:
            return buy
        if buy.status not in {"filled", "partially_filled"}:
            continue
        if (buy.filled_quantity or 0) <= 0:
            continue
        exit_order = by_id.get(buy.exit_order_id or "")
        if exit_order and exit_order.status in {
            "pending", "submitted", "partially_filled", "filled",
        }:
            continue
        return buy
    return None


def _trigger_forced_exit(
    queue: ApprovalQueue,
    broker: Broker,
    buy: OrderCandidate,
    reason_label: str,
    detail: str,
    say: Callable[[str], None],
) -> None:
    qty = buy.filled_quantity
    market = buy.request.market
    ticker = buy.request.ticker
    say(f"  🚨 {market}:{ticker} {reason_label} → 시장가 매도 {qty}주 ({detail})")
    try:
        sell = queue.enqueue(
            OrderRequest(
                ticker=ticker,
                market=market,
                side="sell",
                quantity=qty,
                order_type="market",
                limit_price=None,
                reason=f"{reason_label}: {detail}",
            ),
            stop_loss=buy.stop_loss,
            target1=buy.target1,
            target2=buy.target2,
            analysis_signal="Sell",
        )
    except Exception as exc:
        say(f"    ❌ 매도 큐잉 실패: {exc}")
        return
    try:
        queue.update(_replace(buy, exit_order_id=sell.id, exit_reason=reason_label))
    except Exception as exc:
        logger.warning("Failed to link buy %s to forced exit %s: %s", buy.id, sell.id, exc)
    try:
        approved, result = queue.approve(sell.id, broker)
    except Exception as exc:
        say(f"    ❌ 매도 전송 실패: {exc}")
        return
    if approved.status == "submitted":
        say(f"    ✅ 매도 접수 (id={approved.id}, broker_ref={result.broker_order_id})")
    else:
        say(f"    ❌ 매도 거부: {result.message}")


# ── Position sizing ──────────────────────────────────────────────────


def compute_position_size(
    broker: Broker,
    market: Market,
    entry: float,
    stop: float,
    risk_pct: float,
) -> tuple[int, str]:
    """Return ``(quantity, note)``. quantity is 0 when sizing is impossible.

    Sizing rule: risk_capital = total_account_value × (risk_pct / 100); the
    per-share risk is ``entry - stop``. Quantity is also capped by available
    cash so we never queue an order we cannot fund.
    """

    if risk_pct <= 0 or entry <= 0 or stop <= 0:
        return 0, "risk_per_trade_pct/entry/stop 값이 유효하지 않음"
    per_share_risk = entry - stop
    if per_share_risk <= 0:
        return 0, f"진입가({entry:.2f}) ≤ 손절가({stop:.2f}), 사이징 불가"
    if not hasattr(broker, "get_cash_balance"):
        return 0, "브로커가 잔고 조회를 지원하지 않음"
    try:
        balance = broker.get_cash_balance(market)
    except Exception as exc:
        return 0, f"잔고 조회 실패: {exc}"
    risk_capital = balance.total_value * (risk_pct / 100.0)
    qty_from_risk = int(risk_capital // per_share_risk)
    # Cap by available cash. For US trading on KIS paper accounts the broker
    # may report cash=0 when the foreign-currency deposit table is unpopulated;
    # in that case we fall back to (total_value − securities_value) which the
    # KIS broker already estimates from output3.frcr_evlu_tota.
    available_cash = balance.cash
    if available_cash <= 0 and balance.total_value > balance.securities_value:
        available_cash = max(0.0, balance.total_value - balance.securities_value)
    qty_from_cash = int(available_cash // entry) if entry > 0 else 0
    qty = max(0, min(qty_from_risk, qty_from_cash))
    note = (
        f"총평가 {balance.total_value:.0f}{balance.currency} × {risk_pct}% "
        f"= {risk_capital:.0f}{balance.currency} 리스크자본 / "
        f"주당 {per_share_risk:.2f} → {qty_from_risk}주 "
        f"(가용현금 {available_cash:.0f}{balance.currency} 한도 {qty_from_cash}주)"
    )
    return qty, note


# ── Queue ↔ broker reconciliation ─────────────────────────────────────


def reconcile_queue_with_broker(
    queue: ApprovalQueue,
    broker: Broker,
) -> list[OrderCandidate]:
    """Mark bot-held positions as externally closed when the broker disagrees.

    Walks every filled buy in the queue that doesn't already have a completed
    exit, groups by (market, ticker), and queries the broker for actual
    positions. If the broker reports 0 quantity for a ticker the bot still
    considers held, all stale buys for that ticker get a synthetic ``filled``
    sell appended so downstream logic treats them as closed.

    Returns the list of buys that were reconciled this run.
    """
    if not hasattr(broker, "get_positions"):
        return []

    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    holdings: dict[tuple[str, str], list[OrderCandidate]] = {}

    for o in orders:
        if o.request.side != "buy":
            continue
        if o.status not in {"filled", "partially_filled"}:
            continue
        if (o.filled_quantity or 0) <= 0:
            continue
        exit_o = by_id.get(o.exit_order_id or "")
        if exit_o and exit_o.status in {
            "pending", "submitted", "partially_filled", "filled",
        }:
            continue  # Already in flight or closed.
        holdings.setdefault((o.request.market, o.request.ticker), []).append(o)

    if not holdings:
        return []

    broker_qty: dict[tuple[str, str], int] = {}
    for market in {key[0] for key in holdings}:
        try:
            positions = broker.get_positions(market)  # type: ignore[arg-type]
            for p in positions:
                broker_qty[(market, p.ticker)] = p.quantity
        except Exception as exc:
            logger.warning("Reconcile: positions query failed for %s: %s", market, exc)

    reconciled: list[OrderCandidate] = []
    for (market, ticker), buy_list in holdings.items():
        if broker_qty.get((market, ticker), 0) > 0:
            continue  # Broker still holds; nothing to reconcile.
        for buy in buy_list:
            try:
                queue.mark_externally_closed(buy.id, broker_name=broker.name)
                reconciled.append(buy)
            except Exception as exc:
                logger.warning("Failed to mark %s externally closed: %s", buy.id, exc)
    return reconciled


# ── Position management (stop-loss / target take-profit) ─────────────


def manage_open_positions(
    queue: ApprovalQueue,
    broker: Broker,
    provider: DataProvider,
    say: Callable[[str], None],
) -> None:
    """For each filled buy without an active exit, check current price against
    stop_loss/target1 and fire a sell order if a level is breached."""

    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    price_cache: dict[tuple[str, str], float] = {}
    market_cache: dict[str, bool] = {}
    # Snapshot broker positions per market once — sells are validated against
    # this map so we never queue a sell for a ticker the broker doesn't actually
    # hold (would result in a rejection and stale "submitted" entries).
    broker_pos_cache: dict[str, dict[str, int]] = {}

    def _broker_qty(market: str, ticker: str) -> int | None:
        """Return broker-side quantity, or None if positions query failed."""
        if market not in broker_pos_cache:
            try:
                positions = broker.get_positions(market)  # type: ignore[arg-type]
                broker_pos_cache[market] = {p.ticker: p.quantity for p in positions}
            except Exception as exc:
                logger.warning("Position snapshot failed for %s: %s", market, exc)
                broker_pos_cache[market] = {}
                return None
        return broker_pos_cache[market].get(ticker, 0)

    for buy in orders:
        if buy.request.side != "buy":
            continue
        if buy.status not in {"filled", "partially_filled"}:
            continue
        if (buy.filled_quantity or 0) <= 0:
            continue
        if buy.avg_fill_price is None:
            # No confirmed fill price — skip to avoid acting on ghost positions.
            continue

        # Skip if there's already an in-flight or completed sell.
        active_exit = by_id.get(buy.exit_order_id or "")
        if active_exit and active_exit.status in {
            "pending", "submitted", "partially_filled", "filled",
        }:
            continue

        ticker = buy.request.ticker
        market = buy.request.market
        if market not in market_cache:
            status = market_status(market)
            market_cache[market] = status.is_open
            if not status.is_open:
                say(f"🌙 {market} {status.reason} — 보유 포지션 청산 평가 보류")
        if not market_cache[market]:
            continue
        key = (market, ticker)
        if key not in price_cache:
            try:
                candles = provider.get_candles(ticker, market, lookback=220)
            except Exception as exc:
                logger.warning("Price lookup failed for %s:%s: %s", market, ticker, exc)
                continue
            if not candles:
                continue
            price_cache[key] = candles[-1].close
        current = price_cache[key]

        trigger: str | None = None
        order_type: str = "limit"
        limit_price: float | None = round(current, 2)
        if buy.stop_loss is not None and current <= buy.stop_loss:
            trigger = "stop_loss"
            order_type = "market"
            limit_price = None  # Market order on hard stop.
        elif buy.target1 is not None and current >= buy.target1:
            trigger = "target1"
        if not trigger:
            continue

        qty = buy.filled_quantity

        # ── Verify broker actually has the position before queuing a sell. ──
        # If the user manually liquidated the position between iterations the
        # broker will show 0 qty; selling here would be rejected and leave a
        # stale "rejected" record. Reconcile silently and skip.
        actual_qty = _broker_qty(market, ticker)
        if actual_qty == 0:
            try:
                queue.mark_externally_closed(buy.id, broker_name=broker.name)
                say(
                    f"  🔄 {market}:{ticker} 외부 매도 감지 (브로커 보유 0) "
                    f"— 청산 트리거 무시"
                )
            except Exception as exc:
                logger.warning("Failed to mark %s externally closed: %s", buy.id, exc)
            continue
        if actual_qty is not None and actual_qty < qty:
            # Partial manual exit — sell only what the broker still holds.
            say(
                f"  ⚠️ {market}:{ticker} 봇 기록 {qty}주 vs 브로커 보유 {actual_qty}주 "
                f"— 실보유 수량으로 조정"
            )
            qty = actual_qty
        if qty <= 0:
            continue

        reason = (
            f"{trigger} 트리거 · 현재가 {current:.2f} "
            f"(stop={buy.stop_loss}, t1={buy.target1})"
        )
        say(
            f"  💰 {market}:{ticker} {trigger} → 매도 주문 {qty}주 "
            f"(현재 {current:.2f})"
        )
        try:
            sell = queue.enqueue(
                OrderRequest(
                    ticker=ticker,
                    market=market,
                    side="sell",
                    quantity=qty,
                    order_type=order_type,  # type: ignore[arg-type]
                    limit_price=limit_price,
                    reason=reason,
                ),
                stop_loss=buy.stop_loss,
                target1=buy.target1,
                target2=buy.target2,
                analysis_signal="Sell",
            )
        except Exception as exc:
            say(f"    ❌ 매도 큐잉 실패: {exc}")
            continue

        try:
            queue.update(_replace(buy, exit_order_id=sell.id, exit_reason=trigger))
        except Exception as exc:
            logger.warning("Failed to link buy %s to exit %s: %s", buy.id, sell.id, exc)

        try:
            approved, result = queue.approve(sell.id, broker)
        except Exception as exc:
            say(f"    ❌ 매도 전송 실패: {exc}")
            continue
        if approved.status == "submitted":
            say(
                f"    ✅ 매도 접수 (id={approved.id}, "
                f"broker_ref={result.broker_order_id})"
            )
        else:
            say(f"    ❌ 매도 거부: {result.message}")


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
