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
from zoneinfo import ZoneInfo

from alpha_bot.approval import ApprovalQueue
from alpha_bot.approval.queue import order_belongs_to_broker
from alpha_bot.broker.base import supports_tradability_checks
from alpha_bot.auto.analysis import analyze_ticker, make_broker, make_provider
from alpha_bot.auto.guards import daily_loss_exceeded, kill_switch_active
from alpha_bot.auto.position_manager import (
    count_open_positions,
    find_held_buy,
    manage_open_positions,
    reconcile_queue_with_broker,
    should_force_exit,
    trigger_forced_exit,
)
from alpha_bot.auto.sizing import compute_position_size, sizing_base_value, usable_cash
from alpha_bot.config import AppConfig, load_watchlist
from alpha_bot.market_hours import market_status
from alpha_bot.market_regime import get_regime
from alpha_bot.models import OrderRequest
from alpha_bot.notify import notify
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
    from alpha_bot.strategy import analyzer_from_config
    analyzer = analyzer_from_config(config)
    queue = ApprovalQueue(config.approval_queue, protected_tickers=config.protected_tickers)
    broker = make_broker(opts.broker_name)

    try:
        for order in queue.recover_unresolved_orders(broker):
            say(
                f"  🔁 {order.request.market}:{order.request.ticker} "
                f"{order.id} 전송 결과 복구 → {order.status}"
            )
        changed = queue.sync_with_broker(broker)
        for order in changed:
            say(
                f"  🔁 {order.request.market}:{order.request.ticker} "
                f"{order.id} → {order.status} ({order.filled_quantity}/{order.request.quantity})"
            )
    except Exception as exc:
        logger.warning("Order sync failed: %s", exc)

    # Long-settled order groups leave the hot file; every update() rewrites
    # it in full, so size is latency and (at O(n²)) an eventual failure mode.
    try:
        archived = queue.archive_closed_orders()
        if archived:
            say(f"  🗄️ 종결 주문 {archived}건 아카이브 (logs/orders_archive/)")
    except Exception as exc:
        logger.warning("Order archiving failed: %s", exc)

    legacy = queue.unscoped_broker_orders(broker)
    unresolved = queue.unresolved_orders(broker)

    # ── Cancel limit orders that sat unfilled past the freshness window. ──
    # A stale limit buy can fill hours later at a price whose setup no
    # longer exists; kill it and let the next scan re-evaluate.
    try:
        for order in queue.cancel_stale_orders(broker, config.stale_order_minutes):
            say(
                f"  🗑️ {order.request.market}:{order.request.ticker} "
                f"미체결 {config.stale_order_minutes}분 초과 → 주문 취소 ({order.id})"
            )
    except Exception as exc:
        logger.warning("Stale-order sweep failed: %s", exc)

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
        manage_open_positions(
            queue, broker, provider, say,
            protective_stops=config.protective_stop,
            protected_tickers=config.protected_tickers,
        )
    except Exception as exc:
        logger.exception("Position management failed")
        say(f"⚠️ 포지션 모니터링 실패: {exc}")

    # ── Orphan-stop reconciliation (warn-only). ──
    # Detects venue stops the queue does not reference — the residue of the
    # one recovery hole idempotent replay cannot close. Runs on the 5-minute
    # loop, not the tick monitor, because it costs one list call per symbol.
    try:
        from alpha_bot.auto.protective_stops import warn_unreferenced_stops
        warn_unreferenced_stops(queue, broker, say)
    except Exception as exc:
        logger.warning("Orphan-stop sweep failed: %s", exc)

    # ── Kill switch: block ALL new buys while the file exists. ──
    # Exit management above still ran — held positions stay protected even
    # (especially) while the operator has pulled the emergency brake.
    kill_reason = kill_switch_active()
    if kill_reason:
        say(f"🛑 킬스위치 활성 — 신규 매수 전면 중단 ({kill_reason})")
        notify(
            f"🛑 AlphaBot 킬스위치 활성: {kill_reason}\n신규 매수 중단, 보유 포지션 관리는 계속됩니다.",
            dedupe_key="kill_switch",
        )
        return

    if legacy:
        say(
            f"🛑 계좌 미귀속 기존 주문 {len(legacy)}건 — 신규 매수 중단. "
            "현재 계좌 확인 후 명시적 귀속이 필요합니다."
        )
        notify(
            f"🛑 AlphaBot 계좌 미귀속 주문 {len(legacy)}건 발견\n"
            "자동 계좌 추정을 하지 않았으며 신규 매수를 중단합니다.",
            dedupe_key=f"legacy_scope:{broker.name}",
        )
        return
    if unresolved:
        say(
            f"🛑 결과 불명 주문 {len(unresolved)}건 — 중복주문 방지를 위해 신규 매수 중단"
        )
        notify(
            f"🛑 AlphaBot 결과 불명 주문 {len(unresolved)}건\n"
            "브로커 대사 전까지 신규 매수를 중단합니다.",
            dedupe_key=f"unknown_orders:{broker.name}",
        )
        return

    open_count = count_open_positions(queue, broker)
    if open_count >= config.max_positions:
        say(
            f"⚠️ 보유/대기 주문 {open_count}건이 max_positions({config.max_positions}) 도달, "
            "신규 매수 중단"
        )
        return

    rows = load_watchlist(opts.watchlist)
    cooldown = timedelta(hours=opts.cooldown_hours)
    if config.protected_tickers:
        say(f"🔒 보호 종목 (봇 매매 제외): {', '.join(sorted(config.protected_tickers))}")

    market_cache: dict[str, bool] = {}
    regime_cache: dict[str, bool] = {}
    loss_breaker_cache: dict[str, bool] = {}
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

        if ticker.upper() in config.protected_tickers:
            # Skipped before analysis so protected names cost no API calls
            # and never even produce a signal to be tempted by.
            say(f"[{market}:{ticker}] 🔒 보호 종목 — 봇 매매 대상 아님")
            continue

        if market not in market_cache:
            status = market_status(market)
            market_cache[market] = status.is_open
            if not status.is_open:
                say(f"🌙 {market} 시장 {status.reason} — 신규 매수 스킵")
        if not market_cache[market]:
            continue

        rejected, rejection_reason = _rejection_retry_block(
            queue, ticker, market, broker
        )
        if rejected:
            say(f"[{market}:{ticker}] 🚫 주문 재시도 차단 — {rejection_reason}")
            continue

        # ── CANSLIM "M" filter: block new entries in a bearish broad index ──
        if market not in regime_cache:
            regime = get_regime(market)
            regime_cache[market] = regime.is_bullish
            if not regime.is_bullish:
                say(f"🐻 {market} 레짐 약세: {regime.reason}")
        if not regime_cache[market]:
            continue

        # ── Daily-loss circuit breaker: stop digging once today's realized
        # losses hit the configured share of the account. ──
        if market not in loss_breaker_cache:
            tripped, detail = daily_loss_exceeded(
                queue, broker, market, config.daily_loss_limit_pct
            )
            loss_breaker_cache[market] = tripped
            if tripped:
                say(f"🚧 {market} 일일 손실 한도 도달 — 신규 매수 중단 ({detail})")
                notify(
                    f"🚧 AlphaBot 서킷브레이커: {market} 신규 매수 중단\n{detail}",
                    dedupe_key=f"daily_loss:{market}",
                )
        if loss_breaker_cache[market]:
            continue

        try:
            if opts.cooldown_enabled and _on_cooldown(
                queue, ticker, market, cooldown, broker
            ):
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

            held = find_held_buy(queue, market, ticker, broker)
            if held is not None:
                force_exit, detail = should_force_exit(report)
                if force_exit:
                    trigger_forced_exit(
                        queue, broker, held, "악재 청산", detail, say
                    )
                continue  # Already in position — skip new entry regardless of signal

            if report.signal not in {"Buy", "Strong Buy"}:
                continue

            # ── Venue-side tradability gate ──
            # Runs only for actual buy candidates so the extra calls scale
            # with signals, not with watchlist size.
            blocked, block_reason = _tradability_block(broker, ticker, market)
            if blocked:
                say(f"  ⛔ {ticker} 매수 불가 — {block_reason}")
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
                    max_position_pct=config.max_position_pct,
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
                available = usable_cash(bal)
                if estimated_cost > available:
                    say(
                        f"  💸 {ticker} 현금 부족: 필요 ~{estimated_cost:.0f}{bal.currency}, "
                        f"가용 {available:.0f}{bal.currency} — 매수 건너뜀"
                    )
                    continue
                # Position-size cap also guards the fixed-quantity path — a
                # fat-fingered --quantity must not concentrate the account.
                # Same base as auto-sizing (FX-unified portfolio when the
                # broker can price it) so the two caps cannot disagree.
                if config.max_position_pct > 0:
                    base_value, base_label = sizing_base_value(broker, bal)
                    if base_value > 0:
                        budget = base_value * (config.max_position_pct / 100.0)
                        if estimated_cost > budget:
                            say(
                                f"  🧢 {ticker} 포지션 상한 초과: 필요 ~{estimated_cost:.0f}{bal.currency} "
                                f"> 한도 {budget:.0f}{bal.currency} "
                                f"({config.max_position_pct:.0f}% of {base_label}) — 매수 건너뜀"
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
                broker=broker,
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
                notify(
                    f"✅ AlphaBot 매수 주문: {market}:{ticker} {quantity}주 @ {entry_price} "
                    f"({report.signal}, score {report.scoreboard.total}/30, "
                    f"stop {report.trade_plan.stop_loss:.2f})",
                    dedupe_key=f"buy:{market}:{ticker}:{approved.id}",
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
    queue: ApprovalQueue,
    ticker: str,
    market: str,
    cooldown: timedelta,
    broker=None,
) -> bool:
    if cooldown.total_seconds() <= 0:
        return False
    cutoff = datetime.now(timezone.utc) - cooldown
    for order in queue.list_orders():
        if broker is not None and order.broker_instance_id:
            from alpha_bot.approval.queue import order_belongs_to_broker
            if not order_belongs_to_broker(order, broker):
                continue
        if (
            order.request.ticker == ticker
            and order.request.market == market
            and order.request.side == "buy"
            and order.status
            in {
                "pending",
                "submitting",
                "unknown",
                "submitted",
                "partially_filled",
                "partially_filled_cancelled",
                "filled",
            }
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


def _tradability_block(broker, ticker: str, market: str) -> tuple[bool, str]:
    """Ask the venue whether this symbol may be bought right now.

    Fails **closed** per ticker, unlike the regime and LLM gates which fail
    open. Those degrade signal quality; this one guards against buying into a
    delisting or a suspension, where being wrong is not something the exit
    ladder can recover from. An unverifiable symbol just waits for the next
    sweep, so the cost of failing closed is one iteration of delay.
    """

    if not supports_tradability_checks(broker):
        return False, ""
    try:
        reason = broker.tradability_block(ticker, market)
    except Exception as exc:
        logger.warning("Tradability check failed for %s:%s: %s", market, ticker, exc)
        return True, f"거래 가능 여부 확인 실패 (안전상 매수 보류): {exc}"
    return (True, reason) if reason else (False, "")


def _rejection_retry_block(
    queue: ApprovalQueue,
    ticker: str,
    market: str,
    broker,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Apply session block for permanent rejects and backoff transient ones."""

    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    market_zone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
    local_day = clock.astimezone(market_zone).date()
    rejected: list[tuple[datetime, object]] = []
    for order in queue.list_orders():
        if order.status != "rejected" or order.request.side != "buy":
            continue
        if order.request.ticker != ticker or order.request.market != market:
            continue
        if not order_belongs_to_broker(order, broker):
            continue
        stamp = order.submitted_at or order.created_at
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when.astimezone(market_zone).date() == local_day:
            rejected.append((when, order))
    if not rejected:
        return False, ""

    rejected.sort(key=lambda row: row[0])
    last_time, last = rejected[-1]
    retryable = getattr(last, "rejection_retryable", None)
    code = getattr(last, "rejection_code", None) or "broker-rejected"
    if retryable is not True:
        return True, f"영구 오류 {code}; 현재 거래일 종목 차단"

    attempts = sum(
        1
        for _, order in rejected
        if getattr(order, "rejection_retryable", None) is True
    )
    delay_minutes = min(5 * (2 ** max(0, attempts - 1)), 360)
    retry_at = last_time + timedelta(minutes=delay_minutes)
    if clock < retry_at:
        remaining = max(1, int((retry_at - clock).total_seconds() // 60) + 1)
        return (
            True,
            f"일시 오류 {code}; {attempts}회 실패, 약 {remaining}분 후 재시도",
        )
    return False, ""
