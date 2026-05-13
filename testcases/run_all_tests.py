import yfinance as yf
import pandas as pd
import json
import os
from datetime import date
from alpha_bot.models import Candle, FundamentalsQuarter, MarketContext, NewsAssessment
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.backtest import Backtester

def get_real_candles_fixed(ticker, start="2019-01-01"):
    print(f"[{ticker}] 가격 데이터 수집 중...")
    df = yf.download(ticker, start=start, end="2025-12-31")
    
    # Flatten multi-index if exists
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
        
    candles = []
    for dt, row in df.iterrows():
        try:
            o, h, l, c = float(row['Open']), float(row['High']), float(row['Low']), float(row['Close'])
            v = int(row['Volume'])
            candles.append(Candle(dt.date(), o, h, l, c, v))
        except (ValueError, TypeError):
            continue
    return candles

def run_suite():
    analyzer = StrategyAnalyzer(min_score=24, min_rr=1.5)
    tester = Backtester(analyzer)
    results = {}

    suite = [
        {"name": "NVIDIA (NVDA)", "ticker": "NVDA", "market": "US", "desc": "모멘텀"},
        {"name": "Tesla (TSLA)", "ticker": "TSLA", "market": "US", "desc": "변동성"},
        {"name": "Zoom (ZM)", "ticker": "ZM", "market": "US", "desc": "버블붕괴"},
        {"name": "에코프로 (ECOPRO)", "ticker": "086520.KQ", "market": "KR", "desc": "한국광기"}
    ]

    for case in suite:
        print(f"\n🚀 {case['name']} 시뮬레이션 시작")
        candles = get_real_candles_fixed(case['ticker'])
        if not candles: 
            print(f"  [오류] {case['name']} 데이터를 가져오지 못했습니다.")
            continue
            
        fundamentals = [
            FundamentalsQuarter("latest", eps_yoy=150.0, revenue_yoy=80.0, reported_at=date(2024,1,1)),
            FundamentalsQuarter("prev", eps_yoy=50.0, revenue_yoy=30.0, reported_at=date(2021,1,1))
        ]
        context = MarketContext(benchmark_return_3m=5.0, stock_return_3m=10.0)
        
        res = tester.run(case['ticker'], case['market'], candles, fundamentals, [], context)
        
        results[case['name']] = {
            "trades": len(res.trades),
            "return": f"{res.total_return_pct:.1f}%",
            "mdd": f"{res.max_drawdown_pct:.1f}%",
            "sharpe": f"{res.sharpe_ratio:.2f}"
        }
        
        if res.trades:
            for t in res.trades:
                print(f"  {t.entry_date} -> {t.exit_date} [{t.outcome}] {t.return_pct:.1f}%")
        else:
            print("  [알림] 매매 신호 없음 (보수적 리스크 관리 작동)")

    print("\n" + "="*70)
    print(f"{'종목명':<20} | {'매매':<5} | {'수익률':<10} | {'MDD':<10} | {'Sharpe'}")
    print("-"*70)
    for name, data in results.items():
        print(f"{name:<20} | {data['trades']:<5} | {data['return']:<10} | {data['mdd']:<10} | {data['sharpe']}")
    print("="*70)

if __name__ == "__main__":
    run_suite()
