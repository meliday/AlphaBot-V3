import csv
import json
from datetime import date, timedelta

# 1. 가격 데이터 생성 (SK하이닉스 2025.10 ~ 2026.05 가상 리얼리티)
# 2025.10월 약 180,000원에서 시작하여 HBM 호재로 240,000원 돌파 후 조정 시나리오
def create_hynix_data():
    start_date = date(2025, 1, 1) # 충분한 Warm-up을 위해 1월부터 시작
    end_date = date(2026, 5, 11)
    
    price = 175000.0
    candles = []
    day = start_date
    
    while day <= end_date:
        if day.weekday() < 5:
            # 구간별 추세 설정
            if day < date(2025, 10, 1):
                drift = 0.0005 # 완만한 상승
            elif day < date(2026, 2, 1):
                drift = 0.002  # 강력한 HBM 랠리 구간
            elif day < date(2026, 4, 1):
                drift = -0.001 # 기간 조정 및 변동성 수렴(VCP 형성 구간)
            else:
                drift = 0.0015 # 다시 반등 시작
            
            import random
            rng = random.Random(day.toordinal())
            noise = rng.uniform(-0.015, 0.015)
            
            # VCP 구간에서는 변동성(noise)을 줄여서 수렴을 모사
            if date(2026, 3, 1) < day < date(2026, 4, 1):
                noise *= 0.4
                
            price = price * (1 + drift + noise)
            close = round(price, -2) # 백원 단위 반올림
            open_p = round(close * (1 + rng.uniform(-0.005, 0.005)), -2)
            high = round(max(open_p, close) * (1 + rng.uniform(0, 0.01)), -2)
            low = round(min(open_p, close) * (1 - rng.uniform(0, 0.01)), -2)
            volume = int(rng.uniform(2000000, 5000000))
            
            candles.append({
                "date": day.isoformat(),
                "open": open_p, "high": high, "low": low, "close": close, "volume": volume
            })
        day += timedelta(days=1)
    
    # CSV 저장
    import os
    os.makedirs("data/prices", exist_ok=True)
    with open("data/prices/KR_000660.csv", "w", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(candles)

    # 2. 펀더멘털 데이터 (실제 예상 실적 반영)
    fundamentals = [
        {"period": "2026Q1", "eps_yoy": 45.0, "revenue_yoy": 30.0, "reported_at": "2026-04-25"},
        {"period": "2025Q4", "eps_yoy": 38.0, "revenue_yoy": 25.0, "reported_at": "2026-01-25"},
        {"period": "2025Q3", "eps_yoy": 20.0, "revenue_yoy": 15.0, "reported_at": "2025-10-25"}
    ]
    os.makedirs("data/fundamentals", exist_ok=True)
    with open("data/fundamentals/KR_000660.json", "w") as f:
        json.dump(fundamentals, f)

    # 3. 뉴스/카탈리스트
    news = [
        {"headline": "HBM4 양산 계획 발표", "summary": "SK하이닉스, 차세대 HBM 시장 주도권 강화", "published_at": "2026-03-15"},
        {"headline": "1분기 어닝 서프라이즈", "summary": "영업이익 예상치 상회하며 견조한 성장세 증명", "published_at": "2026-04-26"}
    ]
    os.makedirs("data/news", exist_ok=True)
    with open("data/news/KR_000660.json", "w") as f:
        json.dump(news, f)

if __name__ == "__main__":
    create_hynix_data()
    print("SK Hynix (000660) High-Fidelity Data Created.")
