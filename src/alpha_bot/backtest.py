from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

from alpha_bot.models import Candle, Catalyst, FundamentalsQuarter, Market, MarketContext
from alpha_bot.strategy.analyzer import StrategyAnalyzer

logger = logging.getLogger(__name__)


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

    Cost accounting:
        slippage_pct is the round-trip slippage estimate; we charge half on
        each leg (entry up, exit down) so the model is symmetric across buy
        and sell fills. commission_pct is the total round-trip commission
        and is deducted once at trade close.

    Exit pricing:
        We check the open price first to model gap risk — if a stock gaps
        below the stop, the actual fill is the gap open, not the stop level.
        Only when the open is intra-bounds do we use stop/target as the fill.
        This eliminates the "stop = exact stop price" optimistic assumption
        that previously under-stated tail losses.

    Cooldown:
        After an exit, the next entry probe starts the *next* candle. This
        matches the production cooldown of 24h (one trading day), so live
        trade frequency is consistent with backtested frequency.
    """

    def __init__(
        self,
        analyzer: StrategyAnalyzer | None = None,
        max_hold_days: int = 30,
        commission_pct: float = 0.10,
        slippage_pct: float = 0.05,
        risk_free_rate: float = 0.0,  # annual %, e.g. 4.5 for US, 3.0 for KR
    ):
        self.analyzer = analyzer or StrategyAnalyzer()
        self.max_hold_days = max_hold_days
        # Round-trip cost expressed as percentages of trade value.
        self.commission_pct = commission_pct  # e.g. 0.10 → 0.1 %
        self.slippage_pct = slippage_pct      # e.g. 0.05 → 0.05 %
        self.risk_free_rate = risk_free_rate

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
        # Apply half the round-trip slippage to each leg (symmetric model).
        slip_per_leg = (self.slippage_pct / 2) / 100
        index = min(220, max(20, len(candles) // 4))
        while index < len(candles) - 2:
            asof = candles[index].date
            # ── Point-in-time filtering to eliminate look-ahead bias ──
            visible_fundamentals = [
                f for f in fundamentals
                if (f.reported_at and f.reported_at <= asof)
                or (f.reported_at is None and _period_visible_by(f.period, asof))
            ]
            visible_catalysts = [c for c in catalysts if not c.published_at or c.published_at <= asof]
            report = self.analyzer.analyze(
                ticker, market, candles[: index + 1],
                visible_fundamentals, visible_catalysts, context,
            )
            if report.signal not in {"Buy", "Strong Buy"}:
                index += 1
                continue

            entry_candle = candles[index + 1]
            # Buy leg slippage: open price + half the round-trip slip.
            entry = entry_candle.open * (1 + slip_per_leg)
            stop = report.trade_plan.stop_loss
            target = report.trade_plan.target1

            # Walk forward looking for stop/target hits or max-hold expiry.
            max_exit_idx = min(index + self.max_hold_days, len(candles) - 1)
            exit_idx: int | None = None
            exit_price_raw: float | None = None
            outcome: str | None = None
            for j in range(index + 1, max_exit_idx + 1):
                future = candles[j]
                # Gap-down through stop: we fill at the open, not the stop.
                if future.open <= stop:
                    exit_price_raw = future.open
                    outcome = "stop_gap"
                    exit_idx = j
                    break
                # Gap-up through target: fill at the open (conservative — we
                # exit at the actual achievable price, not the target line).
                if future.open >= target:
                    exit_price_raw = future.open
                    outcome = "target_gap"
                    exit_idx = j
                    break
                # Intraday: assume stop hit first when both touched (worst-
                # case ordering — protects against optimistic accounting).
                if future.low <= stop:
                    exit_price_raw = stop
                    outcome = "stop"
                    exit_idx = j
                    break
                if future.high >= target:
                    exit_price_raw = target
                    outcome = "target1"
                    exit_idx = j
                    break

            if exit_idx is None:
                # Max-hold expiry — exit at the close of the final eligible bar.
                exit_idx = max_exit_idx
                exit_price_raw = candles[exit_idx].close
                outcome = "time"

            # Sell leg slippage: subtract half the round-trip slip.
            exit_price = exit_price_raw * (1 - slip_per_leg)
            raw_return = (exit_price / entry - 1) * 100
            # Only commission is left to subtract (slippage already applied
            # per-leg above — no double-counting).
            net_return = raw_return - self.commission_pct

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
            commission_pct=self.commission_pct,
            slippage_pct=self.slippage_pct,
            risk_free_rate=self.risk_free_rate,
        )
        logger.info(
            "Backtest %s:%s — %d trades, win_rate=%.1f%%, return=%.1f%%, "
            "max_dd=%.1f%%, sharpe=%.2f (commission=%.2f%%, slippage=%.2f%%, rf=%.2f%%)",
            market, ticker, len(trades), result.win_rate, result.total_return_pct,
            result.max_drawdown_pct, result.sharpe_ratio,
            self.commission_pct, self.slippage_pct, self.risk_free_rate,
        )
        return result
