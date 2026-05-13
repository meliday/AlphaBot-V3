import yfinance as yf
import csv
import json
import os
from datetime import date, timedelta
from alpha_bot.models import Candle, NewsAssessment
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.data.providers import FixtureDataProvider
from pathlib import Path

def fetch_brk_data_fixed():
    ticker = "BRK-B"
    print(f"Fetching real historical data for {ticker}...")
    df = yf.download(ticker, start="2019-01-01", end="2025-12-31")
    
    os.makedirs("data/prices", exist_ok=True)
    csv_path = "data/prices/US_BRK-B.csv"
    
    with open(csv_path, "w", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for dt, row in df.iterrows():
            # yfinance multi-index column handling
            v = row['Volume']
            if hasattr(v, 'iloc'): v = v.iloc[0]
            writer.writerow([
                dt.date().isoformat(),
                round(float(row['Open']), 2),
                round(float(row['High']), 2),
                round(float(row['Low']), 2),
                round(float(row['Close']), 2),
                int(v)
            ])

def run_brk_management_sim():
    provider = FixtureDataProvider(Path("data"))
    ticker, market = "BRK-B", "US"
    candles = provider.get_candles(ticker, market, lookback=2000)
    fundamentals = provider.get_fundamentals(ticker, market)
    context = provider.get_market_context(ticker, market)
    analyzer = StrategyAnalyzer(min_score=24, min_rr=1.5)

    # 2020년 1월 2일 시점의 인덱스 찾기
    start_idx = 0
    for i, c in enumerate(candles):
        if c.date >= date(2020, 1, 2):
            start_idx = i
            break
    
    shares = 100
    cash = 0.0
    initial_price = candles[start_idx].close
    initial_value = shares * initial_price
    
    in_position = True
    history = []
    
    print(f"--- 💼 BRK-B 100주 관리 시뮬레이션 시작 ({candles[start_idx].date}) ---")
    print(f"초기 가격: ${initial_price:,.2f}, 초기 가치: ${initial_value:,.2f}")

    for i in range(start_idx, len(candles)):
        curr = candles[i]
        # 당일 분석 (전일까지의 데이터 기준)
        report = analyzer.analyze(ticker, market, candles[:i], fundamentals, [], context)
        
        # 관리 로직: 
        # 1. 봇이 'Hold Off' (하락장) 신호를 주면 전량 매도
        # 2. 봇이 'Buy' 신호를 주면 다시 매수
        if in_position:
            if report.signal == "Hold Off":
                cash = shares * curr.open
                shares = 0
                in_position = False
                history.append(f"[{curr.date}] 🐻 폭락 감지 - 전량 매도 (가격: ${curr.open:,.2f})")
        else:
            if report.signal in {"Buy", "Strong Buy"}:
                shares = int(cash / curr.open)
                cash -= shares * curr.open
                in_position = True
                history.append(f"[{curr.date}] 🐂 반등/수렴 포착 - 재매수 (가격: ${curr.open:,.2f}, 수량: {shares}주)")

    final_price = candles[-1].close
    final_value = (shares * final_price) + cash
    passive_value = 100 * final_price
    
    print("\n--- 📜 매매 히스토리 ---")
    for event in history: print(event)
    
    print(f"\n--- 🏁 시뮬레이션 결과 (2020-2025) ---")
    print(f"Passive (존버) 결과: ${passive_value:,.2f} ({((passive_value/initial_value)-1)*100:.1f}%)")
    print(f"Active (봇 관리) 결과: ${final_value:,.2f} ({((final_value/initial_value)-1)*100:.1f}%)")

if __name__ == "__main__":
    fetch_brk_data_fixed()
    run_brk_management_sim()
