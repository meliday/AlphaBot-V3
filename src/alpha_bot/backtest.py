from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from alpha_bot.models import Candle, Catalyst, FundamentalsQuarter, Market, MarketContext
from alpha_bot.risk import TRAIL_ATR_MULT
from alpha_bot.strategy.analyzer import StrategyAnalyzer
from alpha_bot.strategy.indicators import latest_atr

logger = logging.getLogger(__name__)

# 2026 default model: KR sell taxes 0.20% (KOSPI 0.05% transaction tax +
# 0.15% rural special tax; KOSDAQ transaction tax 0.20%) plus an assumed
# 0.015% commission on each leg. US uses 0.01% per leg. Callers can override.
DEFAULT_ROUND_TRIP_COST_PCT: dict[Market, float] = {"KR": 0.23, "US": 0.02}
BACKTEST_LIMITATIONS = [
    "LLM 뉴스 판정과 강제청산 신호는 과거 시점으로 재생하지 않음",
]


def round_trip_cost_pct(market: Market, override: float | None = None) -> float:
    return DEFAULT_ROUND_TRIP_COST_PCT[market] if override is None else override


def _period_visible_by(period: str, asof: date, reporting_lag_days: int = 60) -> bool:
    """Heuristic: if a quarter is "YYYYQn", assume the report is filed ~60 days
    after quarter-end. If the period string is non-standard (e.g. "latest"),
    we conservatively treat it as visible (no filtering)."""
    m = re.match(r"^(\d{4})Q([1-4])$", period.strip())
    if not m:
        return True  # unknown format → can't filter; preserve old behavior
    year, q = int(m.group(1)), int(m.group(2))
    quarter_end_month = q * 3  # Q1→Mar, Q2→Jun, Q3→Sep, Q4→Dec
    # Last day of quarter end month (approx — 28 is safe lower bound)
    quarter_end = date(year, quarter_end_month, 28)
    report_date = quarter_end + timedelta(days=reporting_lag_days)
    return report_date <= asof


def visible_fundamentals(
    fundamentals: list[FundamentalsQuarter], asof: date
) -> list[FundamentalsQuarter]:
    """Point-in-time filter: only quarters actually reported by ``asof``."""
    return [
        f for f in fundamentals
        if (f.reported_at and f.reported_at <= asof)
        or (f.reported_at is None and _period_visible_by(f.period, asof))
    ]


def visible_catalysts(catalysts: list[Catalyst], asof: date) -> list[Catalyst]:
    """Point-in-time filter: only news published by ``asof``."""
    return [c for c in catalysts if not c.published_at or c.published_at <= asof]


def ladder_step(
    bar: Candle,
    *,
    stop: float,
    target1: float,
    target2: float,
    trail: float | None,
    breakeven: float,
    atr_fn,
    split_exits: bool = True,
) -> tuple[list[tuple[str, float, str]], float | None]:
    """Advance the exit ladder by one daily bar.

    The single source of truth for simulated exits, shared by the
    per-ticker ``Backtester`` and the portfolio engine so the two can never
    drift. Mirrors the live position manager: worst-case intra-bar ordering
    (stop before target, trail before target-2), gaps fill at the open,
    the trail arms at max(breakeven, fill − 2×ATR) on the target-1
    scale-out and only ever ratchets up (on the close).

    Returns ``(events, new_trail)`` where each event is
    ``(kind, fill_price, outcome)`` and kind ∈ {"exit_all", "scale_out",
    "exit_runner"}. ``atr_fn`` is called lazily only when the trail needs
    an ATR value.
    """
    if trail is None:
        # ── Phase 1: full position vs hard stop / target-1 ──
        if bar.open <= stop:
            return [("exit_all", bar.open, "stop_gap")], None
        if bar.open >= target1:
            if not split_exits:
                return [("exit_all", bar.open, "target_gap")], None
            fill, outcome1 = bar.open, "target1_gap"
        elif bar.low <= stop:
            return [("exit_all", stop, "stop")], None
        elif bar.high >= target1:
            if not split_exits:
                return [("exit_all", target1, "target1")], None
            fill, outcome1 = target1, "target1"
        else:
            return [], None

        # Target-1 scale-out; same-bar runner checks, worst case first.
        events: list[tuple[str, float, str]] = [("scale_out", fill, outcome1)]
        atr = atr_fn()
        trail = max(breakeven, fill - TRAIL_ATR_MULT * atr)
        if bar.low <= trail:
            events.append(("exit_runner", trail, "trail"))
            return events, trail
        if bar.high >= target2:
            events.append(("exit_runner", target2, "target2"))
            return events, trail
        return events, max(trail, bar.close - TRAIL_ATR_MULT * atr)

    # ── Phase 2: runner half vs trailing stop / target-2 ──
    if bar.open <= trail:
        return [("exit_runner", bar.open, "trail_gap")], trail
    if bar.open >= target2:
        return [("exit_runner", bar.open, "target2_gap")], trail
    if bar.low <= trail:
        return [("exit_runner", trail, "trail")], trail
    if bar.high >= target2:
        return [("exit_runner", target2, "target2")], trail
    return [], max(trail, bar.close - TRAIL_ATR_MULT * atr_fn())


def limit_entry_fill(
    bar: Candle, signal_close: float, entry_limit_pct: float | None
) -> float | None:
    """Fill price for the live-style limit entry on the bar after the signal.

    Live posts a limit buy at ``signal_close × (1 + entry_limit_pct)`` — the
    ``entry_high`` of the trade plan — and stale-cancels it if unfilled. The
    daily-bar approximation:

      * ``open ≤ limit``  → marketable at the open, fills at the open;
      * ``low ≤ limit``   → touched intraday, fills at the limit;
      * otherwise         → gapped away and never came back — **no fill**.

    ``None`` disables the model (legacy always-fill-at-the-open), kept for
    A/B comparison. Shared by the single-ticker and portfolio engines so the
    two cannot drift. This closes the divergence where the backtest banked
    every gap-up day (disproportionately the winners in a momentum system)
    that the live limit order would have missed — 5–64% of days depending on
    the dataset.
    """

    if entry_limit_pct is None:
        return bar.open
    limit = signal_close * (1 + entry_limit_pct)
    if bar.open <= limit:
        return bar.open
    if bar.low <= limit:
        return limit
    return None


@dataclass(frozen=True)
class BacktestTrade:
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    outcome: str
    return_pct: float


@dataclass(frozen=True)
class BacktestResult:
    ticker: str
    market: Market
    trades: list[BacktestTrade]
    commission_pct: float = 0.0
    slippage_pct: float = 0.0
    risk_free_rate: float = 0.0  # annual %, e.g. 4.5 for US, 3.0 for KR
    limitations: list[str] = field(default_factory=lambda: list(BACKTEST_LIMITATIONS))

    @property
    def total_return_pct(self) -> float:
        return sum(trade.return_pct for trade in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for trade in self.trades if trade.return_pct > 0)
        return wins / len(self.trades) * 100

    @property
    def max_drawdown_pct(self) -> float:
        """Maximum peak-to-trough drawdown across trades."""
        if not self.trades:
            return 0.0
        cumulative = 0.0
        peak = 0.0
        worst = 0.0
        for trade in self.trades:
            cumulative += trade.return_pct
            peak = max(peak, cumulative)
            worst = min(worst, cumulative - peak)
        return worst

    @property
    def trading_days_per_year(self) -> int:
        """Market-specific calendar: KRX ≈ 248 days, NYSE/NASDAQ ≈ 252."""
        return 248 if self.market == "KR" else 252

    @property
    def sharpe_ratio(self) -> float:
        """Annualised Sharpe ratio from per-trade returns.

        Annualisation factor = sqrt(trading_days / avg_hold_days). The
        risk-free rate is subtracted per-trade using the same hold period
        so high-frequency results aren't unduly rewarded over a buy-and-hold
        T-bill alternative.
        """
        if len(self.trades) < 2:
            return 0.0
        returns = [trade.return_pct for trade in self.trades]
        avg = sum(returns) / len(returns)
        var = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if math.isclose(std, 0.0):
            return 0.0
        # Average holding period in trading days (per trade).
        hold_days = []
        for t in self.trades:
            try:
                d1 = date.fromisoformat(t.entry_date)
                d2 = date.fromisoformat(t.exit_date)
                hold_days.append(max(1, (d2 - d1).days))
            except (ValueError, TypeError):
                continue
        avg_hold = sum(hold_days) / len(hold_days) if hold_days else 21.0
        days_per_year = self.trading_days_per_year
        # Risk-free rate scaled to the average holding period.
        rf_per_trade = self.risk_free_rate * avg_hold / days_per_year
        ann_factor = math.sqrt(days_per_year / avg_hold)
        return ((avg - rf_per_trade) / std) * ann_factor


class Backtester:
    """Walk-forward signal back-tester with configurable cost model.

    Exit model (``split_exits=True``, the default) mirrors the live
    position manager's ladder:
        phase 1 — full position: hard stop (market) vs target-1;
        phase 2 — after target-1 scales out half, the runner half trails a
        2×ATR stop (ratcheted up on each close, floored at breakeven) until
        the trailing stop or target-2 is hit. ``split_exits=False`` restores
        the old single-stage stop/target1/time model for A/B comparison.

    Cost accounting:
        slippage_pct is the round-trip slippage estimate; we charge half on
        each leg (the buy and every sell leg) so the model stays symmetric.
        commission_pct is the total round-trip commission and is deducted
        once at trade close.

    Exit pricing:
        We check the open price first to model gap risk — if a stock gaps
        below the stop, the actual fill is the gap open, not the stop level.
        Only when the open is intra-bounds do we use stop/target as the fill.
        Within one bar the worst-case ordering is assumed (stop before
        target, trailing stop before target-2).

    Cooldown:
        After an exit, the next entry probe starts the *next* candle. This
        matches the production cooldown of 24h (one trading day), so live
        trade frequency is consistent with backtested frequency.

    Known live/backtest divergences: the LLM news input and intraday quote
    timing cannot be replayed; the backtest also enforces ``max_hold_days``
    while live positions have no time exit. Entries mirror the live limit
    order via :func:`limit_entry_fill` — gap-throughs are skipped, not
    banked.
    """

    def __init__(
        self,
        analyzer: StrategyAnalyzer | None = None,
        max_hold_days: int = 30,
        commission_pct: float | None = None,
        slippage_pct: float = 0.05,
        risk_free_rate: float = 0.0,  # annual %, e.g. 4.5 for US, 3.0 for KR
        split_exits: bool = True,
        entry_limit_pct: float | None = 0.01,
    ):
        self.analyzer = analyzer or StrategyAnalyzer()
        self.max_hold_days = max_hold_days
        # Round-trip cost expressed as percentages of trade value.
        self.commission_pct = commission_pct
        self.slippage_pct = slippage_pct      # e.g. 0.05 → 0.05 %
        self.risk_free_rate = risk_free_rate
        self.split_exits = split_exits
        # Mirrors the live entry: a limit at signal_close × (1+pct), skipped
        # when the next bar gaps beyond it. None = legacy always-fill (A/B).
        self.entry_limit_pct = entry_limit_pct

    def run(
        self,
        ticker: str,
        market: Market,
        candles: list[Candle],
        fundamentals: list[FundamentalsQuarter],
        catalysts: list[Catalyst],
        context: MarketContext,
    ) -> BacktestResult:
        trades: list[BacktestTrade] = []
        commission_pct = round_trip_cost_pct(market, self.commission_pct)
        # Apply half the round-trip slippage to each leg (symmetric model).
        slip_per_leg = (self.slippage_pct / 2) / 100
        index = min(220, max(20, len(candles) // 4))
        while index < len(candles) - 2:
            asof = candles[index].date
            report = self.analyzer.analyze(
                ticker, market, candles[: index + 1],
                # Point-in-time filtering to eliminate look-ahead bias.
                visible_fundamentals(fundamentals, asof),
                visible_catalysts(catalysts, asof),
                context,
                # Never consult today's market state (regime veto, live
                # benchmark) while judging a historical signal — that leaks
                # the future into the past and makes results depend on the
                # day the backtest happens to run.
                use_live_market_data=False,
            )
            if report.signal not in {"Buy", "Strong Buy"}:
                index += 1
                continue

            entry_candle = candles[index + 1]
            fill = limit_entry_fill(
                entry_candle, candles[index].close, self.entry_limit_pct
            )
            if fill is None:
                # Gapped past the limit; live would stale-cancel and rescan.
                index += 1
                continue
            # Buy leg slippage: fill price + half the round-trip slip.
            entry = fill * (1 + slip_per_leg)
            max_exit_idx = min(index + self.max_hold_days, len(candles) - 1)

            legs, exit_idx, outcome = self._walk_exits(
                candles,
                start_idx=index + 1,
                max_exit_idx=max_exit_idx,
                entry=entry,
                stop=report.trade_plan.stop_loss,
                target1=report.trade_plan.target1,
                target2=report.trade_plan.target2,
            )

            # Each sell leg pays half the round-trip slip; weights sum to 1,
            # so the blended exit is a true per-share average. Commission is
            # deducted once per round trip (slippage already applied per leg).
            exit_price = sum(w * price * (1 - slip_per_leg) for w, price in legs)
            raw_return = (exit_price / entry - 1) * 100
            net_return = raw_return - commission_pct

            trades.append(
                BacktestTrade(
                    entry_date=entry_candle.date.isoformat(),
                    exit_date=candles[exit_idx].date.isoformat(),
                    entry=round(entry, 4),
                    exit=round(exit_price, 4),
                    outcome=outcome,
                    return_pct=round(net_return, 4),
                )
            )
            # Next probe begins the day after the actual exit — matches the
            # production 24h cooldown rather than the old fixed 30-day jump.
            index = exit_idx + 1

        result = BacktestResult(
            ticker.upper(), market, trades,
            commission_pct=commission_pct,
            slippage_pct=self.slippage_pct,
            risk_free_rate=self.risk_free_rate,
        )
        return self._finish(result, ticker, market)

    def _walk_exits(
        self,
        candles: list[Candle],
        start_idx: int,
        max_exit_idx: int,
        entry: float,
        stop: float,
        target1: float,
        target2: float,
    ) -> tuple[list[tuple[float, float]], int, str]:
        """Simulate the live exit ladder over daily bars.

        Returns ``(legs, exit_idx, outcome)`` where each leg is
        ``(weight, raw_fill_price)`` and the weights sum to 1.

        Phase 1 (full position): gap fills at the open; intraday assumes the
        stop is touched before the target (worst-case ordering). On a
        target-1 touch with ``split_exits`` on, half exits and the runner
        phase starts on the same bar — the freshly-armed trail and target-2
        are checked against that bar too (again trail first).

        Phase 2 (runner half): the trail starts at
        max(breakeven, fill − 2×ATR) exactly like the live position manager,
        ratchets up on every close, and never moves down. Whatever is still
        open at ``max_exit_idx`` closes at that bar's close ("time").
        """
        trail: float | None = None
        first_leg_outcome = ""
        legs: list[tuple[float, float]] = []

        for j in range(start_idx, max_exit_idx + 1):
            events, trail = ladder_step(
                candles[j],
                stop=stop,
                target1=target1,
                target2=target2,
                trail=trail,
                breakeven=entry,
                atr_fn=lambda j=j: latest_atr(candles[: j + 1], 14),
                split_exits=self.split_exits,
            )
            for kind, price, outcome in events:
                if kind == "exit_all":
                    return legs + [(1.0, price)], j, outcome
                if kind == "scale_out":
                    legs.append((0.5, price))
                    first_leg_outcome = outcome
                elif kind == "exit_runner":
                    legs.append((0.5, price))
                    return legs, j, f"{first_leg_outcome}+{outcome}"

        # Max-hold expiry — close whatever is still open at the final bar.
        final_close = candles[max_exit_idx].close
        if trail is None:
            return [(1.0, final_close)], max_exit_idx, "time"
        legs.append((0.5, final_close))
        return legs, max_exit_idx, f"{first_leg_outcome}+time"

    def _finish(self, result: BacktestResult, ticker: str, market: Market) -> BacktestResult:
        logger.info(
            "Backtest %s:%s — %d trades, win_rate=%.1f%%, return=%.1f%%, "
            "max_dd=%.1f%%, sharpe=%.2f (commission=%.2f%%, slippage=%.2f%%, "
            "rf=%.2f%%, split_exits=%s)",
            market, ticker, len(result.trades), result.win_rate,
            result.total_return_pct, result.max_drawdown_pct, result.sharpe_ratio,
            result.commission_pct, self.slippage_pct, self.risk_free_rate,
            self.split_exits,
        )
        return result
