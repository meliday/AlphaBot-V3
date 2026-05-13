import yfinance as yf
import csv
import json
import os
from datetime import date

def fetch_brk_data():
    ticker = "BRK-B"
    print(f"Fetching real historical data for {ticker}...")
    
    # 2019년부터 가져와서 Warm-up 기간 확보
    df = yf.download(ticker, start="2019-01-01", end="2025-12-31")
    
    os.makedirs("data/prices", exist_ok=True)
    csv_path = "data/prices/US_BRK-B.csv"
    
    with open(csv_path, "w", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for dt, row in df.iterrows():
            writer.writerow([
                dt.date().isoformat(),
                round(row['Open'], 2),
                round(row['High'], 2),
                round(row['Low'], 2),
                round(row['Close'], 2),
                int(row['Volume'])
            ])
    
    # 버크셔 해서웨이 역사적 실적 (근사치)
    fundamentals = [
        {"period": "2025Q3", "eps_yoy": 15.0, "revenue_yoy": 8.0, "reported_at": "2025-11-05"},
        {"period": "2024Q4", "eps_yoy": 20.0, "revenue_yoy": 10.0, "reported_at": "2025-02-24"},
        {"period": "2023Q4", "eps_yoy": 18.0, "revenue_yoy": 12.0, "reported_at": "2024-02-24"},
        {"period": "2022Q4", "eps_yoy": -5.0, "revenue_yoy": 5.0, "reported_at": "2023-02-25"},
        {"period": "2021Q4", "eps_yoy": 25.0, "revenue_yoy": 15.0, "reported_at": "2022-02-26"},
        {"period": "2020Q4", "eps_yoy": 10.0, "revenue_yoy": 3.0, "reported_at": "2021-02-27"},
        {"period": "2020Q1", "eps_yoy": -80.0, "revenue_yoy": -10.0, "reported_at": "2020-05-02"}
    ]
    os.makedirs("data/fundamentals", exist_ok=True)
    with open("data/fundamentals/US_BRK-B.json", "w") as f:
        json.dump(fundamentals, f)

    # 주요 뉴스 카탈리스트
    news = [
        {"headline": "현금 보유량 사상 최고치 경신", "summary": "버크셔 해서웨이 현금 1600억 달러 돌파", "published_at": "2024-02-24"},
        {"headline": "옥시덴탈 페트롤리움 지분 확대", "summary": "에너지 섹터 비중 강화 지속", "published_at": "2023-06-15"},
        {"headline": "애플 지분 일부 매각 발표", "summary": "포트폴리오 리밸런싱 및 현금 확보", "published_at": "2024-05-04"},
        {"headline": "코로나19 여파로 기록적 분기 손실", "summary": "투자 포트폴리오 가치 하락 반영", "published_at": "2020-05-02"}
    ]
    os.makedirs("data/news", exist_ok=True)
    with open("data/news/US_BRK-B.json", "w") as f:
        json.dump(news, f)

if __name__ == "__main__":
    fetch_brk_data()
    print("BRK-B Real Data Setup Complete.")
