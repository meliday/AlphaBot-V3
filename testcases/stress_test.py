import math
import random
from datetime import date, timedelta
from alpha_bot.models import Candle, MarketContext, FundamentalsQuarter
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.backtest import Backtester

def generate_market_data(regime="bear", lookback=300):
    rng = random.Random(42)
    price = 100.0
    candles = []
    day = date.today() - timedelta(days=lookback + 50)
    
    for i in range(lookback):
        day += timedelta(days=1)
        if day.weekday() >= 5: continue
        
        if regime == "bear":
            drift = -0.003  # 강한 하락 추세
            noise = rng.uniform(-0.02, 0.02)
        elif regime == "sideways":
            drift = 0.0     # 추세 없음
            noise = rng.uniform(-0.03, 0.03) # 높은 변동성
        else:
            drift = 0.001
            noise = rng.uniform(-0.015, 0.015)

        price = max(10.0, price * (1 + drift + noise))
        close = round(price, 2)
        open_p = round(close * (1 + rng.uniform(-0.01, 0.01)), 2)
        high = round(max(open_p, close) * (1 + rng.uniform(0, 0.02)), 2)
        low = round(min(open_p, close) * (1 - rng.uniform(0, 0.02)), 2)
        volume = int(rng.uniform(500000, 1500000))
        candles.append(Candle(day, open_p, high, low, close, volume))
    return candles

def run_stress_test():
    analyzer = StrategyAnalyzer(min_score=24, min_rr=1.5)
    tester = Backtester(analyzer)
    
    # 공통 데이터 (중립)
    fundamentals = [FundamentalsQuarter("latest", eps_yoy=25.0, revenue_yoy=20.0)]
    context = MarketContext(benchmark_return_3m=5.0, stock_return_3m=15.0)

    print("=== 🐻 하락장 시뮬레이션 (Bear Market) ===")
    bear_candles = generate_market_data("bear", lookback=500)
    bear_res = tester.run("BEAR", "US", bear_candles, fundamentals, [], context)
    print_res(bear_res)

    print("\n=== 🎢 진동/횡보장 시뮬레이션 (Sideways/Choppy) ===")
    side_candles = generate_market_data("sideways", lookback=500)
    side_res = tester.run("SIDE", "US", side_candles, fundamentals, [], context)
    print_res(side_res)

    print("\n=== 🐂 상승장 시뮬레이션 (Bull Market) ===")
    bull_candles = generate_market_data("bull", lookback=500)
    bull_res = tester.run("BULL", "US", bull_candles, fundamentals, [], context)
    print_res(bull_res)

def print_res(res):
    print(f"결과: 매매 {len(res.trades)}건, 수익률 {res.total_return_pct:.1f}%, MDD {res.max_drawdown_pct:.1f}%")
    for t in res.trades:
        print(f"  {t.entry_date} -> {t.exit_date} [{t.outcome}] 리턴: {t.return_pct:.1f}%")

if __name__ == "__main__":
    run_stress_test()
