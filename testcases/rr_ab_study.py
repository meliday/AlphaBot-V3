"""A/B study: the R/R basis change vs the threshold set tuned on the old basis.

Context. ``rr_ratio`` used to be measured at the close while every order is
posted at ``entry_high`` (close×1.01); commit 9c2dbb6 fixed the basis. But
``min_rr=1.5``, the high-conviction floors (1.2/1.0), ``strong_buy_rr=3.0``
and the high-R/R score relaxation (≥3.0) were all tuned against the OLD,
inflated numbers. Keeping them unchanged is a silent tightening of 25–90%
depending on stop width.

This script answers the calibration question empirically: for every dataset
we can load (synthetic demo basket + local fixtures), run the Backtester
under

  * OLD   — rr at close, min_rr 1.5  (the behaviour the thresholds were tuned on)
  * NEW@x — rr at entry_high, min_rr ∈ {1.5, 1.3, 1.2, 1.0}

and report trades / win rate / net return / max drawdown, so the new
``min_rr`` can be chosen to preserve the old *effective* strictness rather
than guessed from a single stop-width mapping.

Run:  PYTHONPATH=src python3.12 testcases/rr_ab_study.py
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import replace
from pathlib import Path

from alpha_bot.backtest import Backtester
from alpha_bot.data import FixtureDataProvider, SyntheticDataProvider
from alpha_bot.models import MarketContext
from alpha_bot.risk import build_trade_plan as _real_build_trade_plan, calculate_rr
from alpha_bot.strategy import analyzer as analyzer_module
from alpha_bot.strategy.analyzer import StrategyAnalyzer

# A deliberately mixed basket: the deterministic synthetic generator gives
# every ticker its own seed, so this spans calm and volatile shapes.
SYNTHETIC = [
    ("NVDA", "US"), ("AAPL", "US"), ("TSLA", "US"), ("AMD", "US"),
    ("MSFT", "US"), ("META", "US"), ("PLTR", "US"), ("COIN", "US"),
    ("005930", "KR"), ("000660", "KR"),
]

FIXTURE_DIRS = [Path("data"), Path("testcases/data")]

VARIANTS = [
    ("OLD@1.5", "close", 1.5),
    ("NEW@1.5", "entry", 1.5),
    ("NEW@1.3", "entry", 1.3),
    ("NEW@1.2", "entry", 1.2),
    ("NEW@1.0", "entry", 1.0),
]


def _old_basis_build_trade_plan(candles, sma50, min_rr=1.5):
    """The pre-9c2dbb6 behaviour: rr measured at the close."""

    plan = _real_build_trade_plan(candles, sma50, min_rr)
    return replace(
        plan,
        rr_ratio=calculate_rr(plan.entry_reference, plan.stop_loss, plan.target1),
    )


@contextlib.contextmanager
def rr_basis(mode: str):
    """Swap the analyzer's imported build_trade_plan for the study duration."""

    if mode == "entry":
        yield
        return
    original = analyzer_module.build_trade_plan
    analyzer_module.build_trade_plan = _old_basis_build_trade_plan
    try:
        yield
    finally:
        analyzer_module.build_trade_plan = original


def collect_datasets():
    datasets = []
    synthetic = SyntheticDataProvider()
    for ticker, market in SYNTHETIC:
        datasets.append((
            f"demo:{market}:{ticker}",
            market,
            ticker,
            synthetic.get_candles(ticker, market, lookback=320),
            synthetic.get_fundamentals(ticker, market),
            synthetic.get_catalysts(ticker, market),
            synthetic.get_market_context(ticker, market),
        ))
    for base in FIXTURE_DIRS:
        prices = base / "prices"
        if not prices.is_dir():
            continue
        provider = FixtureDataProvider(base)
        for csv_path in sorted(prices.glob("*_*.csv")):
            market, _, ticker = csv_path.stem.partition("_")
            if market not in {"KR", "US"}:
                continue  # e.g. the JP_N225 study fixture
            try:
                candles = provider.get_candles(ticker, market, lookback=320)
            except Exception as exc:
                print(f"  (skip {csv_path}: {exc})")
                continue
            fundamentals = catalysts = []
            context = MarketContext()
            try:
                fundamentals = provider.get_fundamentals(ticker, market)
            except Exception:
                pass
            try:
                catalysts = provider.get_catalysts(ticker, market)
            except Exception:
                pass
            try:
                context = provider.get_market_context(ticker, market)
            except Exception:
                pass
            datasets.append((
                f"{base}:{market}:{ticker}", market, ticker,
                candles, fundamentals, catalysts, context,
            ))
    return datasets


def main() -> None:
    datasets = collect_datasets()
    print(f"datasets: {len(datasets)}\n")
    header = f"{'dataset':34s} " + "".join(f"{name:>22s}" for name, *_ in VARIANTS)
    print(header)
    print("-" * len(header))

    totals = {name: [0, 0.0] for name, *_ in VARIANTS}  # trades, return-sum
    for name, market, ticker, candles, fundamentals, catalysts, context in datasets:
        cells = []
        for variant, mode, min_rr in VARIANTS:
            with rr_basis(mode), contextlib.redirect_stderr(io.StringIO()):
                result = Backtester(StrategyAnalyzer(min_rr=min_rr)).run(
                    ticker, market, candles, fundamentals, catalysts, context,
                )
            trades = len(result.trades)
            totals[variant][0] += trades
            totals[variant][1] += result.total_return_pct
            cells.append(
                f"{trades:3d}t {result.win_rate:3.0f}% {result.total_return_pct:+6.1f}%"
            )
        print(f"{name:34s} " + "".join(f"{c:>22s}" for c in cells))

    print("-" * len(header))
    footer = f"{'TOTAL (trades / Σreturn)':34s} "
    for variant, *_ in VARIANTS:
        trades, ret = totals[variant]
        footer += f"{f'{trades:3d}t {ret:+7.1f}%':>22s}"
    print(footer)


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    main()
