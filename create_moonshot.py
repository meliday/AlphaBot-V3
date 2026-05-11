import csv
import json
from datetime import date, timedelta

def create_moonshot_data():
    start_date = date(2025, 1, 1)
    end_date = date(2026, 5, 11)
    
    price = 150000.0
    candles = []
    day = start_date
    
    while day <= end_date:
        if day.weekday() < 5:
            # 2025.10월부터 폭등 시작 (Exponential Growth)
            if day < date(2025, 10, 1):
                drift = 0.001
            else:
                # 18만 -> 180만까지 가기 위한 일일 수익률 계산 (약 150거래일 동안 10배)
                # (1+r)^150 = 10 -> r = 10^(1/150) - 1 = 0.015 (매일 1.5%씩 상승)
                drift = 0.016 
            
            import random
            rng = random.Random(day.toordinal())
            # 폭등장에서는 변동성(noise)도 커짐
            noise = rng.uniform(-0.02, 0.04) 
                
            price = price * (1 + drift + noise)
            close = round(price, -2)
            open_p = round(close * (1 + rng.uniform(-0.01, 0.01)), -2)
            high = round(max(open_p, close) * (1 + rng.uniform(0, 0.03)), -2)
            low = round(min(open_p, close) * (1 - rng.uniform(0, 0.02)), -2)
            volume = int(rng.uniform(3000000, 10000000))
            
            candles.append({
                "date": day.isoformat(),
                "open": open_p, "high": high, "low": low, "close": close, "volume": volume
            })
        day += timedelta(days=1)
    
    import os
    os.makedirs("data/prices", exist_ok=True)
    with open("data/prices/KR_000660.csv", "w", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(candles)

    # 펀더멘털은 최상으로 설정
    fundamentals = [
        {"period": "2026Q1", "eps_yoy": 300.0, "revenue_yoy": 150.0, "reported_at": "2026-04-25"},
        {"period": "2025Q4", "eps_yoy": 200.0, "revenue_yoy": 100.0, "reported_at": "2026-01-25"}
    ]
    with open("data/fundamentals/KR_000660.json", "w") as f:
        json.dump(fundamentals, f)

if __name__ == "__main__":
    create_moonshot_data()
    print("SK Hynix 1.8M Moonshot Data Created.")
