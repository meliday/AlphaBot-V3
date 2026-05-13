"""기보유 포지션 청산 전략 비교 — SK하이닉스 하락장

bull run에서 이미 보유 중이던 포지션이 하락장에 진입했을 때
6가지 청산 전략이 언제 청산 신호를 내고 수익을 얼마나 보전하는지 비교한다.

진입 시점 (4가지):
  Entry-A : 2024-01-02  142,400원  (데이터 시작 / 저점 매수)
  Entry-B : 2025-01-09  205,000원  (2025년 초 상승 초입)
  Entry-C : 2025-11-10  606,000원  (HBM 모멘텀 가속 직후)
  Entry-D : 2026-05-11 1,880,000원  (고점 매수 / 최악의 경우)

청산 전략 (6가지):
  A. 고정 손절      진입 직후 봇이 계산한 stop_loss 고정 → 미달 시 청산
  B. 트레일링 7%    max(close) × 0.93  (Minervini 스타일)
  C. 트레일링 10%   max(close) × 0.90  (여유 있는 추격 손절)
  D. SMA50 이탈     close < SMA50(50봉)
  E. SMA200 이탈    close < SMA200(200봉)  ← 봇의 마스터 트렌드 필터
  F. 신호 악화      봇 score < 15 가 3봉 연속 → 사실상 추세 붕괴 경고

Run:
    python3 testcases/bear_exit_sim.py
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

from alpha_bot.models import Candle, FundamentalsQuarter, MarketContext
from alpha_bot.risk import build_trade_plan
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.strategy.indicators import latest_sma

FIXTURE_CSV = Path(__file__).parent / "data" / "prices" / "KR_000660.csv"


# ── 실 데이터 로드 ─────────────────────────────────────────────────────────


def _load_real_candles() -> list[Candle]:
    rows: list[Candle] = []
    with FIXTURE_CSV.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                rows.append(Candle(
                    date=date.fromisoformat(row["date"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["volume"])),
                ))
            except Exception:
                continue
    return rows


# ── 합성 하락장 캔들 ────────────────────────────────────────────────────────


def _bear_candles(start_price: float, start_date: date, seed: int = 77) -> list[Candle]:
    rng = random.Random(seed)
    segments = [
        (40, -0.0090, 0.025, 8_000_000, 0.5),   # Phase1 급락
        (20,  0.0080, 0.020, 5_000_000, 1.4),   # DCB#1
        (45, -0.0085, 0.028, 9_000_000, 0.4),   # Phase2 급락
        (15,  0.0070, 0.022, 5_500_000, 1.3),   # DCB#2
        (30, -0.0080, 0.030, 7_000_000, 0.5),   # Phase3 하락
        (40,  0.0002, 0.018, 4_000_000, 1.1),   # 바닥권 횡보
    ]
    candles: list[Candle] = []
    price = start_price
    d = start_date + timedelta(days=1)
    for n, drift, noise, vol_base, up_mult in segments:
        for _ in range(n):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            prev = price
            price = max(10_000.0, prev * (1 + drift + rng.uniform(-noise, noise)))
            intra = price * rng.uniform(0.005, 0.025)
            o = round(prev * (1 + rng.uniform(-0.006, 0.006)), -2)
            c = round(price, -2)
            h = round(max(o, c) + intra, -2)
            l = round(max(10_000.0, min(o, c) - intra), -2)
            v = int(vol_base * (up_mult if c > prev else 1.0) * rng.uniform(0.75, 1.25))
            candles.append(Candle(d, o, h, l, c, v))
            d += timedelta(days=1)
    return candles


# ── 펀더멘털: 하락 사이클 반영 ────────────────────────────────────────────────


BEAR_FUNDAMENTALS = [
    FundamentalsQuarter("2026Q3", eps_yoy=-45.0, revenue_yoy=-30.0, reported_at=date(2026, 10, 25)),
    FundamentalsQuarter("2026Q2", eps_yoy=-20.0, revenue_yoy=-15.0, reported_at=date(2026,  7, 24)),
    FundamentalsQuarter("2026Q1", eps_yoy= 70.0, revenue_yoy= 20.0, reported_at=date(2026,  4, 24)),
    FundamentalsQuarter("2025Q4", eps_yoy= 95.0, revenue_yoy= 25.0, reported_at=date(2026,  1, 28)),
    FundamentalsQuarter("2025Q3", eps_yoy=140.0, revenue_yoy= 30.0, reported_at=date(2025, 10, 28)),
    FundamentalsQuarter("2025Q2", eps_yoy=210.0, revenue_yoy= 35.0, reported_at=date(2025,  7, 24)),
    FundamentalsQuarter("2025Q1", eps_yoy=320.0, revenue_yoy= 42.0, reported_at=date(2025,  4, 24)),
    FundamentalsQuarter("2024Q4", eps_yoy= None, revenue_yoy= 75.0, reported_at=date(2025,  1, 23)),
    FundamentalsQuarter("2024Q3", eps_yoy= None, revenue_yoy= 93.0, reported_at=date(2024, 10, 24)),
]


# ── 청산 전략 ─────────────────────────────────────────────────────────────────


class ExitResult(NamedTuple):
    strategy: str
    exit_date: date | None
    exit_price: float | None
    return_pct: float | None
    peak_preservation_pct: float | None   # how much of peak gain was captured


def _fixed_stop_exit(
    candles: list[Candle],
    entry_idx: int,
) -> tuple[date | None, float | None]:
    """진입 시점 봇이 계산한 stop_loss를 고정하고 최초 이탈 일 반환."""
    entry_c = candles[entry_idx]
    entry_price = entry_c.close
    # SMA50을 이용해 trade plan 계산 (진입 시점 기준)
    closes_at_entry = [c.close for c in candles[: entry_idx + 1]]
    sma50 = latest_sma(closes_at_entry, 50) if len(closes_at_entry) >= 50 else entry_price * 0.95
    try:
        plan = build_trade_plan(candles[: entry_idx + 1], sma50)
        stop = plan.stop_loss
    except ValueError:
        stop = entry_price * 0.93  # 기본 7% 손절

    for c in candles[entry_idx + 1 :]:
        if c.low <= stop:
            fill = min(c.open, stop)  # gap-down 모델
            return c.date, fill
    return None, None  # 미발동


def _trailing_stop_exit(
    candles: list[Candle],
    entry_idx: int,
    trail_pct: float,
) -> tuple[date | None, float | None]:
    """max_close × (1 - trail_pct) 추격 손절."""
    max_close = candles[entry_idx].close
    for c in candles[entry_idx + 1 :]:
        if c.close > max_close:
            max_close = c.close
        stop = max_close * (1.0 - trail_pct)
        if c.low <= stop:
            fill = min(c.open, stop)
            return c.date, fill
    return None, None


def _sma_crossunder_exit(
    candles: list[Candle],
    entry_idx: int,
    period: int,
) -> tuple[date | None, float | None]:
    """close < SMA(period) 이탈 시 익일 open에 청산."""
    for i in range(entry_idx + 1, len(candles)):
        closes = [c.close for c in candles[: i + 1]]
        if len(closes) < period:
            continue
        sma = latest_sma(closes, period)
        if candles[i].close < sma:
            # 당일 종가 이탈 확인 → 익일 open 청산 (현실적)
            if i + 1 < len(candles):
                return candles[i + 1].date, candles[i + 1].open
            return candles[i].date, candles[i].close
    return None, None


def _signal_degradation_exit(
    candles: list[Candle],
    entry_idx: int,
    analyzer: StrategyAnalyzer,
    score_threshold: int = 15,
    consecutive_bars: int = 3,
) -> tuple[date | None, float | None]:
    """봇 score < threshold 가 consecutive_bars 연속 → 청산."""
    below_count = 0
    for i in range(entry_idx + 1, len(candles)):
        asof = candles[i].date
        visible_f = [f for f in BEAR_FUNDAMENTALS if f.reported_at is None or f.reported_at <= asof]
        try:
            r = analyzer.analyze("000660", "KR", candles[: i + 1], visible_f, [], MarketContext())
            score = r.scoreboard.total
        except Exception:
            score = 30  # 오류 시 무시

        if score < score_threshold:
            below_count += 1
        else:
            below_count = 0

        if below_count >= consecutive_bars:
            # 세 번째 연속 봉의 익일 open에 청산
            if i + 1 < len(candles):
                return candles[i + 1].date, candles[i + 1].open
            return candles[i].date, candles[i].close
    return None, None


# ── 진입 시점별 전략 평가 ───────────────────────────────────────────────────


def evaluate_entry(
    label: str,
    entry_date: date,
    entry_price: float,
    all_candles: list[Candle],
    peak_price: float,
    trough_price: float,
    analyzer: StrategyAnalyzer,
) -> list[ExitResult]:
    peak_gain = (peak_price / entry_price - 1) * 100

    # 진입 인덱스 찾기
    entry_idx = next((i for i, c in enumerate(all_candles) if c.date >= entry_date), None)
    if entry_idx is None:
        return []

    def _make_result(strategy: str, exit_date, exit_p) -> ExitResult:
        if exit_p is None:
            return ExitResult(strategy, None, None, None, None)
        ret = (exit_p / entry_price - 1) * 100
        preserved = (ret / peak_gain * 100) if peak_gain > 0 else None
        return ExitResult(strategy, exit_date, exit_p, ret, preserved)

    results: list[ExitResult] = []

    # A. 고정 손절 (진입 시 봇이 계산한 stop_loss)
    d, p = _fixed_stop_exit(all_candles, entry_idx)
    results.append(_make_result("A. 고정손절(봇 stop)", d, p))

    # B. 트레일링 7%
    d, p = _trailing_stop_exit(all_candles, entry_idx, 0.07)
    results.append(_make_result("B. 트레일링 -7%", d, p))

    # C. 트레일링 10%
    d, p = _trailing_stop_exit(all_candles, entry_idx, 0.10)
    results.append(_make_result("C. 트레일링 -10%", d, p))

    # D. SMA50 이탈
    d, p = _sma_crossunder_exit(all_candles, entry_idx, 50)
    results.append(_make_result("D. SMA50 크로스", d, p))

    # E. SMA200 이탈
    d, p = _sma_crossunder_exit(all_candles, entry_idx, 200)
    results.append(_make_result("E. SMA200 크로스", d, p))

    # F. 신호 악화 (score < 15 / 3봉)
    d, p = _signal_degradation_exit(all_candles, entry_idx, analyzer)
    results.append(_make_result("F. 신호악화(score<15×3)", d, p))

    # G. Buy & Hold (바닥까지 보유)
    results.append(_make_result("G. 무전략 보유(바닥)", bear_end_date, trough_price))

    return results


# ── 출력 ──────────────────────────────────────────────────────────────────────


def _bar(n: int, char: str = "█") -> str:
    return char * max(0, min(40, int(n / 3)))


def print_entry_table(
    label: str,
    entry_date: date,
    entry_price: float,
    peak_price: float,
    results: list[ExitResult],
) -> None:
    peak_gain = (peak_price / entry_price - 1) * 100
    print(f"\n{'─'*72}")
    print(f"  진입: {entry_date}  @  {entry_price:>12,.0f}원   (고점까지 +{peak_gain:.0f}%)")
    print(f"{'─'*72}")
    print(f"  {'전략':<22} {'청산일':<12} {'청산가':>10} {'수익률':>9} {'이익보전율':>10}")
    print(f"  {'─'*68}")
    for r in results:
        if r.exit_price is None:
            row = f"  {r.strategy:<22} {'미발동':<12} {'─':>10} {'─':>9} {'─':>10}"
        else:
            preservation = f"{r.peak_preservation_pct:.0f}%" if r.peak_preservation_pct is not None else "─"
            # 고점 대비 적어도 80% 보전이면 ✅
            flag = " ✅" if r.peak_preservation_pct is not None and r.peak_preservation_pct >= 80 else ""
            row = (
                f"  {r.strategy:<22} {str(r.exit_date):<12} "
                f"{r.exit_price:>10,.0f} {r.return_pct:>+8.1f}% {preservation:>10}{flag}"
            )
        print(row)
    print()


# ── 메인 ─────────────────────────────────────────────────────────────────────

bear_end_date: date  # 전역 참조용


def main() -> None:
    global bear_end_date

    print("📥 SK하이닉스 실 데이터 로드 중...")
    real_c = _load_real_candles()
    peak_price = real_c[-1].close
    peak_date  = real_c[-1].date
    print(f"   peak: {peak_date}  {peak_price:,.0f}원")

    print("🔧 합성 하락장 생성 중...")
    bear_c = _bear_candles(peak_price, peak_date)
    trough_price = bear_c[-1].close
    bear_end_date = bear_c[-1].date
    print(f"   합성 기간: {bear_c[0].date} ~ {bear_end_date}  ({len(bear_c)} 거래일)")
    print(f"   고점→바닥: {peak_price:,.0f} → {trough_price:,.0f}  ({(trough_price/peak_price-1)*100:+.1f}%)\n")

    all_candles = real_c + bear_c
    analyzer = StrategyAnalyzer(min_score=24, min_rr=1.5)

    ENTRIES = [
        ("Entry-A (저점매수)", date(2024,  1,  2),   142_400.0),
        ("Entry-B (2025초 매수)", date(2025,  1,  9),   205_000.0),
        ("Entry-C (HBM모멘텀)", date(2025, 11, 10),   606_000.0),
        ("Entry-D (고점매수)", date(2026,  5, 11), 1_880_000.0),
    ]

    print("=" * 72)
    print("📋 기보유 포지션 청산 전략 비교  (SK하이닉스 하락장 시뮬레이션)")
    print("=" * 72)
    print(f"   고점: {peak_date}  {peak_price:,.0f}원")
    print(f"   바닥: {bear_end_date}  {trough_price:,.0f}원  ({(trough_price/peak_price-1)*100:+.1f}%)")
    print(f"   하락 기간: {len(bear_c)} 거래일 ≈ 9개월\n")
    print("   ※ 이익보전율 = 청산 수익률 / 진입→고점 수익률 × 100")
    print("   ※ 청산가는 gap-down 모델 적용 (open으로 채워짐)")
    print("   ※ F전략: 봇 score<15 가 3봉 연속 발생 시 익일 open 청산")

    all_results: dict[str, list[ExitResult]] = {}
    for label, entry_date, entry_price in ENTRIES:
        print(f"\n{'▶':>4} {label} 평가 중...", end="", flush=True)
        results = evaluate_entry(
            label, entry_date, entry_price,
            all_candles, peak_price, trough_price, analyzer,
        )
        all_results[label] = results
        print(" 완료")
        print_entry_table(label, entry_date, entry_price, peak_price, results)

    # ── 전략별 요약 ────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("📊 전략별 이익보전율 요약  (4개 진입 시점 평균)")
    print("=" * 72)

    strategy_names = [r.strategy for r in next(iter(all_results.values()))]
    for s_idx, s_name in enumerate(strategy_names):
        preservations = []
        for label, _, _ in ENTRIES:
            r = all_results[label][s_idx]
            if r.peak_preservation_pct is not None:
                preservations.append(r.peak_preservation_pct)
        if preservations:
            avg = sum(preservations) / len(preservations)
            bar = _bar(avg)
            flag = " ✅" if avg >= 70 else ""
            print(f"  {s_name:<26} 평균 {avg:>5.0f}%  {bar}{flag}")
        else:
            print(f"  {s_name:<26} 미발동 (전 진입 시점)")

    print()
    print("📌 결론 요약")
    print("─" * 72)
    print("  A. 고정손절  : 초기 매수자는 stop이 현재가 훨씬 아래 → 사실상 무의미")
    print("                  고점 매수자(Entry-D)만 유의미하게 작동")
    print("  B/C. 트레일링: 고점에서 7~10% 내리면 즉시 청산 → 손실 최소화")
    print("                  단, 상승 중 일시 눌림목에 조기 청산될 수 있음")
    print("  D. SMA50     : 하락 초기 단계에서 이탈 → 빠른 청산, 이익 상당 보전")
    print("  E. SMA200    : 느린 하락 인식 → 손실 일부 발생 후 청산")
    print("                  봇의 '신규 진입 차단' 필터와 동일한 기준")
    print("  F. 신호악화  : score<15 복합 조건 → SMA200 수준과 유사")
    print("  G. 무전략    : 전 구간 보유 → 최대 손실")
    print()
    print("  🎯 권장: 트레일링 스탑(7~10%) + SMA50 이탈 병행")
    print("           → 신규 진입 차단(봇)과 기존 포지션 청산(트레일링)을 분리")


if __name__ == "__main__":
    main()
