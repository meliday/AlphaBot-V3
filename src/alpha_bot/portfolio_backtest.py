"""Portfolio-level backtest: the whole auto-pilot, replayed day by day.

The per-ticker ``Backtester`` answers "is this signal any good?" — but the
live system never trades one name in isolation: positions compete for the
same cash, ``max_positions`` caps concurrency, and risk-based sizing keys
off *current* equity. This engine replays a universe on a shared calendar
with exactly those constraints so the strategy is judged as a system:

  * signals evaluated on each ticker's bar close (point-in-time
    fundamentals/catalysts, ``use_live_market_data=False``);
  * entries fill at the NEXT bar's open, gated by free slots and cash,
    sized like ``auto/sizing.py`` (risk % of equity, capped by cash and
    ``max_position_pct``);
  * exits walk the same ladder as live/backtest via
    ``backtest.ladder_step`` (stop → target-1 scale-out → 2×ATR trail →
    target-2 / time), with share-level scale-outs (larger half first,
    matching the live position manager);
  * equity curve marked to market daily → max drawdown and Sharpe come
    from the portfolio path, not per-trade returns.

Single-currency by design: all tickers in one run must share a market.
Mixing KRW and USD cash in one pool would be meaningless — run one
portfolio per market instead (the CLI does this grouping).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from alpha_bot.backtest import (
    BACKTEST_LIMITATIONS,
    ladder_step,
    round_trip_cost_pct,
    visible_catalysts,
    visible_fundamentals,
)
from alpha_bot.models import Candle, Catalyst, FundamentalsQuarter, Market, MarketContext
from alpha_bot.strategy.analyzer import StrategyAnalyzer
from alpha_bot.strategy.indicators import latest_atr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TickerSeries:
    """Everything the analyzer needs for one universe member."""

    ticker: str
    market: Market
    candles: list[Candle]
    fundamentals: list[FundamentalsQuarter] = field(default_factory=list)
    catalysts: list[Catalyst] = field(default_factory=list)
    context: MarketContext = field(default_factory=MarketContext)


@dataclass(frozen=True)
class PortfolioTrade:
    ticker: str
    entry_date: str
    exit_date: str
    shares: int
    entry: float
    exit: float          # share-weighted average across legs (net of slippage)
    outcome: str
    return_pct: float    # net of slippage and commission
    pnl: float           # currency P&L including commission


@dataclass(frozen=True)
class PortfolioBacktestResult:
    market: Market
    starting_cash: float
    ending_equity: float
    trades: list[PortfolioTrade]
    equity_curve: list[tuple[str, float]]  # (iso date, marked-to-market equity)
    max_concurrent_positions: int
    skipped_entries: int  # signals that could not fill (slots/cash/size=0)
    commission_pct: float = 0.0
    limitations: list[str] = field(default_factory=lambda: list(BACKTEST_LIMITATIONS))

    @property
    def total_return_pct(self) -> float:
        if self.starting_cash <= 0:
            return 0.0
        return (self.ending_equity / self.starting_cash - 1) * 100

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.return_pct > 0)
        return wins / len(self.trades) * 100

    @property
    def max_drawdown_pct(self) -> float:
        """Peak-to-trough drawdown of the marked-to-market equity curve."""
        peak = -math.inf
        worst = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                worst = min(worst, (equity / peak - 1) * 100)
        return worst

    @property
    def sharpe_ratio(self) -> float:
        """Annualised Sharpe from daily equity returns (rf = 0)."""
        values = [eq for _, eq in self.equity_curve]
        rets = [
            v / prev - 1
            for prev, v in zip(values, values[1:])
            if prev > 0
        ]
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        if math.isclose(std, 0.0):
            return 0.0
        days = 248 if self.market == "KR" else 252
        return mean / std * math.sqrt(days)


@dataclass
class _Position:
    ticker: str
    entry_idx: int          # bar index in the ticker's candle list
    entry_date: str
    entry_price: float      # includes buy-leg slippage
    shares: int             # remaining shares
    initial_shares: int
    stop: float
    target1: float
    target2: float
    trail: float | None = None
    first_leg_outcome: str = ""
    legs: list[tuple[int, float]] = field(default_factory=list)  # (qty, net price)


@dataclass
class _PendingEntry:
    ticker: str
    fill_idx: int           # ticker bar index at which the entry fills (next open)
    stop: float
    target1: float
    target2: float


class PortfolioBacktester:
    """Day-by-day multi-ticker simulation under shared capital constraints."""

    def __init__(
        self,
        analyzer: StrategyAnalyzer | None = None,
        *,
        starting_cash: float = 10_000.0,
        max_positions: int = 5,
        risk_per_trade_pct: float = 1.0,
        max_position_pct: float = 20.0,
        max_hold_days: int = 30,
        commission_pct: float | None = None,
        slippage_pct: float = 0.05,
    ):
        self.analyzer = analyzer or StrategyAnalyzer()
        self.starting_cash = starting_cash
        self.max_positions = max_positions
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_pct = max_position_pct
        self.max_hold_days = max_hold_days
        self.commission_pct = commission_pct
        self.slippage_pct = slippage_pct

    def run(self, universe: list[TickerSeries]) -> PortfolioBacktestResult:
        if not universe:
            raise ValueError("Portfolio backtest needs at least one ticker series.")
        markets = {s.market for s in universe}
        if len(markets) > 1:
            raise ValueError(
                f"One portfolio = one market/currency; got {sorted(markets)}. "
                "Run one portfolio per market."
            )
        market = universe[0].market
        commission_pct = round_trip_cost_pct(market, self.commission_pct)
        slip = (self.slippage_pct / 2) / 100  # per leg

        series = {s.ticker: s for s in universe}
        # Per-ticker date→index map plus each ticker's signal warm-up
        # (identical to the single-ticker Backtester's starting index).
        idx_by_date: dict[str, dict[str, int]] = {}
        warmup: dict[str, int] = {}
        for s in universe:
            idx_by_date[s.ticker] = {c.date.isoformat(): i for i, c in enumerate(s.candles)}
            warmup[s.ticker] = min(220, max(20, len(s.candles) // 4))

        calendar = sorted({c.date for s in universe for c in s.candles})

        cash = self.starting_cash
        positions: dict[str, _Position] = {}
        pending: dict[str, _PendingEntry] = {}
        last_close: dict[str, float] = {}
        last_exit_idx: dict[str, int] = {}
        trades: list[PortfolioTrade] = []
        equity_curve: list[tuple[str, float]] = []
        max_concurrent = 0
        skipped_entries = 0

        def close_position(pos: _Position, exit_date: str, outcome: str) -> None:
            nonlocal cash
            entry_value = pos.entry_price * pos.initial_shares
            proceeds = sum(qty * price for qty, price in pos.legs)
            commission = entry_value * (commission_pct / 100)
            cash -= commission
            pnl = proceeds - entry_value - commission
            avg_exit = proceeds / pos.initial_shares
            trades.append(
                PortfolioTrade(
                    ticker=pos.ticker,
                    entry_date=pos.entry_date,
                    exit_date=exit_date,
                    shares=pos.initial_shares,
                    entry=round(pos.entry_price, 4),
                    exit=round(avg_exit, 4),
                    outcome=outcome,
                    return_pct=round(pnl / entry_value * 100, 4),
                    pnl=round(pnl, 4),
                )
            )

        for day in calendar:
            day_iso = day.isoformat()

            # ── 1. Exits (free cash before entries the same day) ──
            for ticker in list(positions):
                pos = positions[ticker]
                s = series[ticker]
                bar_idx = idx_by_date[ticker].get(day_iso)
                if bar_idx is None:
                    continue
                bar = s.candles[bar_idx]
                events, pos.trail = ladder_step(
                    bar,
                    stop=pos.stop,
                    target1=pos.target1,
                    target2=pos.target2,
                    trail=pos.trail,
                    breakeven=pos.entry_price,
                    atr_fn=lambda i=bar_idx, c=s.candles: latest_atr(c[: i + 1], 14),
                )
                closed = False
                outcome = ""
                for kind, price, label in events:
                    net = price * (1 - slip)
                    if kind == "scale_out":
                        # Larger half off, runner keeps the rest — same share
                        # split as the live position manager.
                        qty = (pos.shares + 1) // 2
                        if qty >= pos.shares:  # 1-share position: full exit
                            qty = pos.shares
                            closed, outcome = True, label
                        else:
                            pos.first_leg_outcome = label
                        pos.legs.append((qty, net))
                        pos.shares -= qty
                        cash += qty * net
                        if pos.shares == 0 and not closed:
                            closed, outcome = True, label
                    else:  # exit_all / exit_runner
                        qty = pos.shares
                        pos.legs.append((qty, net))
                        pos.shares = 0
                        cash += qty * net
                        closed = True
                        outcome = (
                            f"{pos.first_leg_outcome}+{label}"
                            if pos.first_leg_outcome
                            else label
                        )
                # Max-hold time expiry (after ladder had its chance today).
                if not closed and bar_idx >= min(
                    pos.entry_idx + self.max_hold_days, len(s.candles) - 1
                ):
                    qty = pos.shares
                    net = bar.close * (1 - slip)
                    pos.legs.append((qty, net))
                    pos.shares = 0
                    cash += qty * net
                    closed = True
                    outcome = (
                        f"{pos.first_leg_outcome}+time" if pos.first_leg_outcome else "time"
                    )
                if closed:
                    close_position(pos, day_iso, outcome)
                    del positions[ticker]
                    last_exit_idx[ticker] = bar_idx

            # ── 2. Fill pending entries at today's open ──
            for ticker in list(pending):
                order = pending[ticker]
                bar_idx = idx_by_date[ticker].get(day_iso)
                if bar_idx is None:
                    continue
                if bar_idx != order.fill_idx:
                    # Calendar drifted past the intended bar (data gap) —
                    # the setup is stale; drop the order.
                    del pending[ticker]
                    continue
                del pending[ticker]
                if len(positions) >= self.max_positions:
                    skipped_entries += 1
                    continue
                bar = series[ticker].candles[bar_idx]
                entry = bar.open * (1 + slip)
                per_share_risk = entry - order.stop
                if per_share_risk <= 0:
                    skipped_entries += 1
                    continue
                equity = cash + sum(
                    p.shares * last_close.get(p.ticker, p.entry_price)
                    for p in positions.values()
                )
                qty = int((equity * self.risk_per_trade_pct / 100) // per_share_risk)
                qty = min(qty, int(cash // entry))
                if self.max_position_pct > 0:
                    qty = min(qty, int((equity * self.max_position_pct / 100) // entry))
                if qty < 1:
                    skipped_entries += 1
                    continue
                cash -= qty * entry
                positions[ticker] = _Position(
                    ticker=ticker,
                    entry_idx=bar_idx,
                    entry_date=day_iso,
                    entry_price=entry,
                    shares=qty,
                    initial_shares=qty,
                    stop=order.stop,
                    target1=order.target1,
                    target2=order.target2,
                )
                max_concurrent = max(max_concurrent, len(positions))

            # ── 3. Evaluate signals on today's close → queue next-open entry ──
            for s in universe:
                ticker = s.ticker
                bar_idx = idx_by_date[ticker].get(day_iso)
                if bar_idx is None:
                    continue
                last_close[ticker] = s.candles[bar_idx].close
                if ticker in positions or ticker in pending:
                    continue
                if bar_idx < warmup[ticker] or bar_idx >= len(s.candles) - 2:
                    continue
                # One-bar cooldown after an exit, matching the per-ticker
                # Backtester's re-entry cadence.
                if bar_idx <= last_exit_idx.get(ticker, -1):
                    continue
                try:
                    report = self.analyzer.analyze(
                        ticker, s.market, s.candles[: bar_idx + 1],
                        visible_fundamentals(s.fundamentals, day),
                        visible_catalysts(s.catalysts, day),
                        s.context,
                        use_live_market_data=False,
                    )
                except Exception as exc:
                    logger.warning("Portfolio analyze failed for %s @ %s: %s", ticker, day_iso, exc)
                    continue
                if report.signal not in {"Buy", "Strong Buy"}:
                    continue
                pending[ticker] = _PendingEntry(
                    ticker=ticker,
                    fill_idx=bar_idx + 1,
                    stop=report.trade_plan.stop_loss,
                    target1=report.trade_plan.target1,
                    target2=report.trade_plan.target2,
                )

            # ── 4. Mark to market ──
            equity = cash + sum(
                p.shares * last_close.get(p.ticker, p.entry_price)
                for p in positions.values()
            )
            equity_curve.append((day_iso, round(equity, 4)))

        # Liquidate anything still open at the final close so the result is
        # fully realized (mirrors "stop the bot and flatten").
        final_iso = calendar[-1].isoformat()
        for ticker, pos in list(positions.items()):
            price = last_close.get(ticker, pos.entry_price) * (1 - slip)
            pos.legs.append((pos.shares, price))
            cash += pos.shares * price
            pos.shares = 0
            outcome = (
                f"{pos.first_leg_outcome}+end" if pos.first_leg_outcome else "end"
            )
            close_position(pos, final_iso, outcome)
            del positions[ticker]
        if equity_curve:
            equity_curve[-1] = (final_iso, round(cash, 4))

        result = PortfolioBacktestResult(
            market=market,
            starting_cash=self.starting_cash,
            ending_equity=round(cash, 4),
            trades=trades,
            equity_curve=equity_curve,
            max_concurrent_positions=max_concurrent,
            skipped_entries=skipped_entries,
            commission_pct=commission_pct,
        )
        logger.info(
            "Portfolio backtest %s — %d tickers, %d trades, return=%.2f%%, "
            "max_dd=%.2f%%, sharpe=%.2f, max_concurrent=%d, skipped=%d",
            market, len(universe), len(trades), result.total_return_pct,
            result.max_drawdown_pct, result.sharpe_ratio,
            max_concurrent, skipped_entries,
        )
        return result
