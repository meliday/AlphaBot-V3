"""반도체 버블 붕괴 + 하락장 시뮬레이션

SK하이닉스 실데이터(2024-01 ~ 2026-05-11, peak 1,880,000원)에
합성 하락장 구간을 이어붙여 봇의 반응을 검증한다.

하락장 설계 (역사적 반도체 사이클 참고):
  Phase 0 : 실 데이터 bull run (2024-01 ~ 2026-05-11)
  Phase 1 : 초기 급락  -30% / 40일  (수요 둔화 공포 + 美 수출 규제)
  DCB  #1 : 반등      +17% / 20일  (숏 스퀴즈, 과매도 반등)
  Phase 2 : 2차 하락  -33% / 45일  (어닝 미스 + 가이던스 컷)
  DCB  #2 : 반등      +11% / 15일  (정책 기대감)
  Phase 3 : 3차 하락  -22% / 30일  (수요 회복 지연, 섹터 로테이션)
  바닥권  : 횡보       ±4% / 40일  (accumulation 여부 판단 구간)

총 합성 기간 : ~190 영업일 ≈ 9개월
peak-to-trough : 약 -63%  (2022년 삼성/하이닉스 -60% 전례 참고)

Run:
    python3 testcases/semiconductor_bear.py
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from alpha_bot.backtest import BacktestResult, Backtester
from alpha_bot.models import Candle, FundamentalsQuarter, MarketContext
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.strategy.indicators import latest_sma


# ── 실 데이터 로드 ─────────────────────────────────────────────────────

def _load_real_candles() -> list[Candle]:
    df = yf.download("000660.KS", start="2024-01-01", end="2026-05-12",
                     progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    candles: list[Candle] = []
    for dt, row in df.iterrows():
        try:
            candles.append(Candle(dt.date(), float(row["Open"]), float(row["High"]),
                                  float(row["Low"]), float(row["Close"]), int(row["Volume"])))
        except Exception:
            continue
    return candles


# ── 합성 하락장 캔들 생성 ──────────────────────────────────────────────

def _bear_candles(
    start_price: float,
    start_date: date,
    seed: int = 77,
) -> list[Candle]:
    """실 데이터 마지막 캔들 바로 다음 날부터 이어붙일 합성 구간."""
    rng = random.Random(seed)

    # (n_bars, drift_per_bar, noise_amp, vol_base, up_day_vol_mult, label)
    segments = [
        # Phase 1 : 초기 급락 (거래량 폭증, 공포)
        (40, -0.0090, 0.025, 8_000_000, 0.5, "Phase1 급락"),
        # DCB #1 : 숏 스퀴즈 반등
        (20,  0.0080, 0.020, 5_000_000, 1.4, "DCB#1 반등"),
        # Phase 2 : 2차 하락 (어닝 미스, 가이던스 컷)
        (45, -0.0085, 0.028, 9_000_000, 0.4, "Phase2 급락"),
        # DCB #2 : 정책 기대 반등
        (15,  0.0070, 0.022, 5_500_000, 1.3, "DCB#2 반등"),
        # Phase 3 : 3차 하락 (섹터 로테이션, 수요 회복 지연)
        (30, -0.0080, 0.030, 7_000_000, 0.5, "Phase3 하락"),
        # 바닥권 횡보
        (40,  0.0002, 0.018, 4_000_000, 1.1, "바닥권 횡보"),
    ]

    candles: list[Candle] = []
    price = start_price
    d = start_date + timedelta(days=1)
    labels: list[tuple[date, str]] = []

    for n, drift, noise, vol_base, up_mult, label in segments:
        phase_start = None
        for _ in range(n):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            if phase_start is None:
                phase_start = d
                labels.append((d, label))

            prev = price
            price = max(10_000.0, prev * (1 + drift + rng.uniform(-noise, noise)))
            intra = price * rng.uniform(0.005, 0.025)   # 하락장 고변동성
            o = round(prev * (1 + rng.uniform(-0.006, 0.006)), -2)
            c = round(price, -2)
            h = round(max(o, c) + intra, -2)
            l = round(max(10_000, min(o, c) - intra), -2)
            v = int(vol_base * (up_mult if c > prev else 1.0) * rng.uniform(0.75, 1.25))
            candles.append(Candle(d, o, h, l, c, v))
            d += timedelta(days=1)

    return candles, labels


# ── 펀더멘털: 반도체 다운사이클 반영 ────────────────────────────────────

BEAR_FUNDAMENTALS = [
    # 가이던스 컷 + 어닝 미스 (하락장 후반)
    FundamentalsQuarter("2026Q3", eps_yoy=-45.0, revenue_yoy=-30.0, reported_at=date(2026, 10, 25)),
    FundamentalsQuarter("2026Q2", eps_yoy=-20.0, revenue_yoy=-15.0, reported_at=date(2026, 7, 24)),
    # 직전 bull run 실적 (아직 메모리에 남아있는 좋은 숫자)
    FundamentalsQuarter("2026Q1", eps_yoy= 70.0, revenue_yoy= 20.0, reported_at=date(2026, 4, 24)),
    FundamentalsQuarter("2025Q4", eps_yoy= 95.0, revenue_yoy= 25.0, reported_at=date(2026, 1, 28)),
    FundamentalsQuarter("2025Q3", eps_yoy=140.0, revenue_yoy= 30.0, reported_at=date(2025, 10, 28)),
]


# ── 시그널 스냅샷 ────────────────────────────────────────────────────────

def _snapshot(
    label: str,
    candles: list[Candle],
    funds: list[FundamentalsQuarter],
    analyzer: StrategyAnalyzer,
    asof_date: date,
) -> None:
    visible = [f for f in funds if f.reported_at is None or f.reported_at <= asof_date]
    visible_c = [c for c in candles if c.date <= asof_date]
    if len(visible_c) < 20:
        return
    r = analyzer.analyze("000660", "KR", visible_c, visible, [], MarketContext())
    ind = r.indicators
    sma200 = latest_sma([c.close for c in visible_c], 200)
    gap_pct = (ind.close / sma200 - 1) * 100
    sb = r.scoreboard
    print(
        f"  {asof_date}  [{label:<14}]  "
        f"close={ind.close/1000:>7.0f}k  "
        f"SMA200={sma200/1000:>7.0f}k({gap_pct:+.1f}%)  "
        f"Signal={r.signal:<8}  "
        f"Score={sb.total:>2}(F={sb.fundamentals},T={sb.technical_trend},M={sb.momentum_flow})  "
        f"rr={r.trade_plan.rr_ratio:.2f}"
    )


# ── 메인 ─────────────────────────────────────────────────────────────────

def main() -> None:
    print("📥 SK하이닉스 실 데이터 로드 중...")
    real_c = _load_real_candles()
    peak_price = real_c[-1].close
    peak_date  = real_c[-1].date
    print(f"   peak: {peak_date}  {peak_price:,.0f}원\n")

    print("🔧 합성 하락장 구간 생성 중...")
    bear_c, phase_labels = _bear_candles(peak_price, peak_date)
    trough_price = bear_c[-1].close
    ptrough_pct  = (trough_price / peak_price - 1) * 100
    print(f"   합성 기간: {bear_c[0].date} ~ {bear_c[-1].date}  ({len(bear_c)} 거래일)\n")

    all_candles = real_c + bear_c

    # ── Phase별 가격 추이 ──────────────────────────────────────────────
    print("=" * 80)
    print("📉 하락장 시나리오 설계")
    print("=" * 80)
    prev_price = peak_price
    for start_date, label in phase_labels:
        end_c = next((c for c in reversed(bear_c) if c.date < (start_date + timedelta(days=200))
                      and c.date >= start_date), None)
        # 다음 phase 시작 전까지의 마지막 캔들
        idx = next((i for i, c in enumerate(bear_c) if c.date >= start_date), None)
        if idx is None:
            continue
        next_label_date = next((ld for ld, ll in phase_labels if ld > start_date), bear_c[-1].date + timedelta(days=1))
        phase_candles = [c for c in bear_c if start_date <= c.date < next_label_date]
        if not phase_candles:
            continue
        end_price = phase_candles[-1].close
        chg = (end_price / prev_price - 1) * 100
        abs_chg = (end_price / peak_price - 1) * 100
        print(f"  {label:<14}: {prev_price/1000:>7.0f}k → {end_price/1000:>7.0f}k  "
              f"({chg:+.1f}% / 고점대비 {abs_chg:+.1f}%)")
        prev_price = end_price

    print(f"\n  고점대비 최대낙폭(MDD) : {ptrough_pct:+.1f}%")
    print(f"  peak  : {peak_price:,.0f}원 ({peak_date})")
    print(f"  trough: {trough_price:,.0f}원 ({bear_c[-1].date})")

    # ── 시그널 스냅샷 (주요 시점마다) ──────────────────────────────────
    analyzer = StrategyAnalyzer(min_score=24, min_rr=1.5)
    print("\n" + "=" * 80)
    print("🔍 시점별 봇 신호 (점수·SMA200 대비 위치 포함)")
    print("=" * 80)
    print(f"  {'날짜':<12}  {'구간':<16}  {'현재가':>8}  {'SMA200(괴리)':>14}  {'신호':<8}  {'점수':>18}  {'R:R'}")

    # bull run 마지막 시점
    _snapshot("Bull-Peak", all_candles, BEAR_FUNDAMENTALS, analyzer, peak_date)

    # 각 phase 시작 직후 + DCB 고점 근처
    for start_date, label in phase_labels:
        snap_dates = [start_date + timedelta(days=d) for d in [5, 15]]
        for sd in snap_dates:
            # 해당 날짜에 가장 가까운 영업일
            actual = next((c.date for c in all_candles if c.date >= sd), None)
            if actual:
                _snapshot(label, all_candles, BEAR_FUNDAMENTALS, analyzer, actual)

    # 바닥권 마지막
    _snapshot("바닥권", all_candles, BEAR_FUNDAMENTALS, analyzer, bear_c[-1].date)

    # ── 백테스트 ────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("🧪 봇 백테스트 (하락장 전체 구간)")
    print("=" * 80)
    tester = Backtester(analyzer, max_hold_days=30, commission_pct=0.25, slippage_pct=0.15, risk_free_rate=3.0)
    bear_start = bear_c[0].date
    result = tester.run("000660", "KR", all_candles, BEAR_FUNDAMENTALS, [], MarketContext())
    bear_trades = [t for t in result.trades if date.fromisoformat(t.entry_date) >= bear_start]

    if not bear_trades:
        print("  진입 신호 없음 — 하락장 전 구간 현금 보유 (봇이 거래를 거부)")
    else:
        wins = [t for t in bear_trades if t.return_pct > 0]
        print(f"  {'Entry':<12} {'Exit':<12} {'Outcome':<12} {'Return':>8}")
        print("  " + "-" * 50)
        for t in bear_trades:
            print(f"  {t.entry_date:<12} {t.exit_date:<12} {t.outcome:<12} {t.return_pct:>7.2f}%")
        print("  " + "-" * 50)
        print(f"  Win rate : {len(wins)}/{len(bear_trades)} ({len(wins)/len(bear_trades)*100:.1f}%)")
        print(f"  Sum P/L  : {sum(t.return_pct for t in bear_trades):+.2f}%")

    # ── Buy & Hold 비교 ─────────────────────────────────────────────────
    bh_entry = peak_price
    bh_exit  = trough_price
    bh_ret   = (bh_exit / bh_entry - 1) * 100
    bot_ret  = sum(t.return_pct for t in bear_trades)

    print("\n" + "=" * 80)
    print("📊 자본 보존 비교")
    print("=" * 80)
    print(f"  하락장 진입 시점 (고점) : {peak_price:,.0f}원")
    print(f"  하락장 종료 시점 (바닥) : {trough_price:,.0f}원")
    print()
    print(f"  Buy & Hold      : {bh_ret:+.1f}%  (고점에 매수하고 버텼을 때)")
    print(f"  봇 전략          : {bot_ret:+.2f}%  (진입 거부 = 0% 손실)")
    print()

    if len(bear_trades) == 0:
        print("  ✅ 봇은 SMA200 마스터 트렌드 필터로 하락장 전 구간 신규 진입 차단")
        print("     → 고점 대비 -63% 낙폭 구간에서 자본 100% 보존")
        print()
        print("  하락장에서 봇이 차단되는 핵심 메커니즘:")
        print("  ① close < SMA200 → '마스터 추세 필터' 즉시 Hold Off")
        print("  ② 설령 DCB(반등)이 와도 SMA200은 느리게 하락 → 갭이 유지됨")
        print("  ③ 펀더멘털 악화 (eps_yoy 음수) → 어닝 캐션 플래그 추가 방어선")
        print()
        peak_sma200 = latest_sma([c.close for c in all_candles if c.date <= peak_date], 200)
        for label, approx_date_offset in [("DCB#1 고점", 60), ("DCB#2 고점", 120), ("바닥권", 185)]:
            target_date = peak_date + timedelta(days=approx_date_offset)
            visible = [c for c in all_candles if c.date <= target_date]
            if len(visible) >= 200:
                sma = latest_sma([c.close for c in visible], 200)
                close_at = visible[-1].close
                gap = (close_at / sma - 1) * 100
                print(f"  {label}: close={close_at/1000:.0f}k  SMA200={sma/1000:.0f}k  괴리={gap:+.1f}%", end="")
                print("  → Hold Off ✓" if close_at < sma else "  → SMA200 돌파 (주의)")
    else:
        print(f"  봇이 {len(bear_trades)}건 진입 (하락장 내 신호 발생)")
        print(f"  자본 변화: {100 + bot_ret:.1f}%  (B&H 대비 {'우위' if bot_ret > bh_ret else '열위'})")


if __name__ == "__main__":
    main()
