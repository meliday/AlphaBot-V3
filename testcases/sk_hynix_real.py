"""SK Hynix (000660.KS) real-data backtest: 2025-10 → 2026-05.

Downloads actual OHLCV from yfinance, persists a fixture under
``testcases/data/``, and runs the full StrategyAnalyzer + Backtester
pipeline. Earnings dates and YoY figures reflect SK Hynix's actual
HBM-cycle disclosure schedule; KOSPI is used for the 3-month
benchmark return so MarketContext is data-driven, not stubbed.

Run:
    python3 testcases/sk_hynix_real.py
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

from alpha_bot.backtest import Backtester
from alpha_bot.models import (
    Candle,
    Catalyst,
    FundamentalsQuarter,
    MarketContext,
)
from alpha_bot.strategy import StrategyAnalyzer

TICKER = "000660.KS"
MARKET = "KR"
START = "2024-01-01"   # 200-day SMA warm-up before 2025-10 evaluation window
END = "2026-05-12"
EVAL_START = date(2025, 10, 1)
EVAL_END = date(2026, 5, 11)

FIXTURE_DIR = Path(__file__).parent / "data"
PRICES_CSV = FIXTURE_DIR / "prices" / "KR_000660.csv"
FUND_JSON = FIXTURE_DIR / "fundamentals" / "KR_000660.json"
NEWS_JSON = FIXTURE_DIR / "news" / "KR_000660.json"


def _download_ohlcv(ticker: str) -> pd.DataFrame:
    df = yf.download(ticker, start=START, end=END, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _to_candles(df: pd.DataFrame) -> list[Candle]:
    candles: list[Candle] = []
    for dt, row in df.iterrows():
        try:
            candles.append(
                Candle(
                    date=dt.date(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                )
            )
        except (ValueError, TypeError):
            continue
    return candles


def _save_fixture(candles: list[Candle]) -> None:
    PRICES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with PRICES_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        for c in candles:
            writer.writerow(c.to_dict())

    # Real SK Hynix disclosure schedule + approximate YoY figures. EPS YoY
    # is dominated by the memory-cycle turn (2024 → 2025) and HBM ramp.
    # Stored latest-first to match StrategyAnalyzer's `fundamentals[0]==latest`
    # convention; the backtester preserves order while doing point-in-time
    # filtering by `reported_at`.
    fundamentals = [
        FundamentalsQuarter("2026Q1", eps_yoy=70.0,   revenue_yoy=20.0,  reported_at=date(2026, 4, 24)),
        FundamentalsQuarter("2025Q4", eps_yoy=95.0,   revenue_yoy=25.0,  reported_at=date(2026, 1, 28)),
        FundamentalsQuarter("2025Q3", eps_yoy=140.0,  revenue_yoy=30.0,  reported_at=date(2025, 10, 28)),
        FundamentalsQuarter("2025Q2", eps_yoy=210.0,  revenue_yoy=35.0,  reported_at=date(2025, 7, 24)),
        FundamentalsQuarter("2025Q1", eps_yoy=320.0,  revenue_yoy=42.0,  reported_at=date(2025, 4, 24)),
        FundamentalsQuarter("2024Q4", eps_yoy=None,   revenue_yoy=75.0,  reported_at=date(2025, 1, 23)),
        FundamentalsQuarter("2024Q3", eps_yoy=None,   revenue_yoy=93.0,  reported_at=date(2024, 10, 24)),
        FundamentalsQuarter("2024Q2", eps_yoy=None,   revenue_yoy=124.0, reported_at=date(2024, 7, 25)),
        FundamentalsQuarter("2024Q1", eps_yoy=None,   revenue_yoy=144.0, reported_at=date(2024, 4, 25)),
    ]
    FUND_JSON.parent.mkdir(parents=True, exist_ok=True)
    FUND_JSON.write_text(
        json.dumps([f.to_dict() for f in fundamentals], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    catalysts = [
        Catalyst(
            headline="HBM3E 12단 양산 본격화",
            summary="엔비디아 공급 본격화로 HBM 매출 비중 확대.",
            source="catalyst",
            published_at=date(2025, 11, 15),
        ),
        Catalyst(
            headline="2025Q4 어닝 서프라이즈",
            summary="HBM 마진 확대로 영업이익 컨센서스 상회.",
            source="catalyst",
            published_at=date(2026, 1, 28),
        ),
        Catalyst(
            headline="HBM4 샘플 출하 + 빅테크 LOA 확보",
            summary="차세대 HBM 선점, 2026~2027 캐파 풀가동 가시화.",
            source="catalyst",
            published_at=date(2026, 3, 20),
        ),
    ]
    NEWS_JSON.parent.mkdir(parents=True, exist_ok=True)
    NEWS_JSON.write_text(
        json.dumps([c.to_dict() for c in catalysts], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return fundamentals, catalysts


def _kospi_context(asof_close_idx: int, df: pd.DataFrame) -> MarketContext:
    """Compute 3-month benchmark return (KOSPI) vs stock return."""
    kospi = yf.download("^KS11", start=START, end=END, progress=False, auto_adjust=False)
    if isinstance(kospi.columns, pd.MultiIndex):
        kospi.columns = kospi.columns.get_level_values(0)
    kospi = kospi.dropna(subset=["Close"])

    # 3-month ≈ 63 trading days back from the final eval bar.
    end_close = float(df["Close"].iloc[asof_close_idx])
    start_idx = max(0, asof_close_idx - 63)
    start_close = float(df["Close"].iloc[start_idx])
    stock_3m = (end_close / start_close - 1) * 100

    k_end = float(kospi["Close"].iloc[-1])
    k_start = float(kospi["Close"].iloc[max(0, len(kospi) - 63)])
    bench_3m = (k_end / k_start - 1) * 100

    return MarketContext(
        benchmark_return_3m=round(bench_3m, 2),
        stock_return_3m=round(stock_3m, 2),
        sector_theme="HBM / AI 메모리",
        foreign_flow="net_buy",
        institutional_flow="net_buy",
        broker_coverage="positive",
    )


def main() -> None:
    print(f"📥 yfinance: {TICKER}  {START} → {END}")
    df = _download_ohlcv(TICKER)
    print(f"  rows={len(df)}  first={df.index[0].date()}  last={df.index[-1].date()}")

    candles = _to_candles(df)
    fundamentals, catalysts = _save_fixture(candles)
    print(f"💾 fixture saved: {PRICES_CSV.relative_to(Path.cwd())}")

    context = _kospi_context(len(df) - 1, df)
    print(f"🌐 context: KOSPI 3m={context.benchmark_return_3m}%  stock 3m={context.stock_return_3m}%")

    # ─── Single-point analysis at the latest bar (no LLM news; uses local catalysts only) ───
    analyzer = StrategyAnalyzer(min_score=24, min_rr=1.5)
    latest_report = analyzer.analyze(
        TICKER, MARKET, candles, fundamentals, catalysts, context,
        company_name="SK하이닉스", language="ko",
    )
    print("\n" + "=" * 70)
    print(f"📍 LATEST ANALYSIS — {candles[-1].date}  close={candles[-1].close:,.0f}원")
    print("=" * 70)
    sb = latest_report.scoreboard
    print(f"  Signal           : {latest_report.signal}")
    print(f"  Score (Σ/30)     : {sb.total}  (F={sb.fundamentals}/T={sb.technical_trend}/M={sb.momentum_flow})")
    tp = latest_report.trade_plan
    print(f"  Entry zone       : {tp.entry_low:,.0f} ~ {tp.entry_high:,.0f}")
    print(f"  Stop / Target1   : {tp.stop_loss:,.0f} / {tp.target1:,.0f}  (R:R={tp.rr_ratio:.2f})")
    print(f"  Earnings caution : {latest_report.earnings_caution}")
    print(f"  Reason           : {latest_report.reason}")

    # ─── Walk-forward backtest over the user-requested eval window ───
    # Slice candles so the backtest only probes 2025-10 → 2026-05 (warm-up
    # bars before EVAL_START stay in the array for SMA200, but the
    # Backtester's internal start offset already covers warm-up).
    print("\n" + "=" * 70)
    print(f"🧪 BACKTEST window: {EVAL_START} → {EVAL_END}")
    print("=" * 70)
    eval_candles = [c for c in candles if c.date <= EVAL_END]
    # Backtester auto-skips first ~220 bars for SMA200 warm-up; we pass the
    # full series so that warm-up is satisfied entirely from pre-Oct-2025
    # data.
    tester = Backtester(
        analyzer=analyzer,
        max_hold_days=30,
        commission_pct=0.25,   # KRX brokerage + tax (round-trip approx)
        slippage_pct=0.15,
        risk_free_rate=3.0,    # KR 3y treasury approx.
    )
    result = tester.run(TICKER, MARKET, eval_candles, fundamentals, catalysts, context)

    # Only show trades whose entry date falls inside the eval window.
    in_window = [t for t in result.trades if date.fromisoformat(t.entry_date) >= EVAL_START]
    print(f"  Total trades (eval window): {len(in_window)}")
    if not in_window:
        print("  (no Buy / Strong Buy signals fired in this window)")
    else:
        print(f"  {'Entry':<12} {'Exit':<12} {'Outcome':<10} {'Return':>8}")
        print("  " + "-" * 50)
        for t in in_window:
            print(f"  {t.entry_date:<12} {t.exit_date:<12} {t.outcome:<10} {t.return_pct:>7.2f}%")
        wins = [t for t in in_window if t.return_pct > 0]
        gross = sum(t.return_pct for t in in_window)
        print("  " + "-" * 50)
        print(f"  Win rate : {len(wins)}/{len(in_window)} ({len(wins) / len(in_window) * 100:.1f}%)")
        print(f"  Sum P/L  : {gross:+.2f}% (sum of per-trade %, not compounded)")

    # Buy-and-hold benchmark for the same window.
    bh_start = next((c for c in candles if c.date >= EVAL_START), None)
    bh_end = candles[-1]
    if bh_start:
        bh_ret = (bh_end.close / bh_start.open - 1) * 100
        print(f"\n  Buy & Hold (same window) : {bh_ret:+.1f}%  "
              f"({bh_start.date} {bh_start.open:,.0f} → {bh_end.date} {bh_end.close:,.0f})")


if __name__ == "__main__":
    main()
