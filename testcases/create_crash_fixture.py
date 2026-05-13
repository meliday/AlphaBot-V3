import csv
import json
import random
from datetime import date, timedelta

def create_covid_crash_data():
    start_date = date(2019, 1, 1) # Warm-up
    end_date = date(2020, 8, 31)
    
    price = 300.0 # SPY-like price
    candles = []
    day = start_date
    
    while day <= end_date:
        if day.weekday() < 5:
            # Phase 1: Pre-Covid steady growth
            if day < date(2020, 2, 20):
                drift = 0.0005
                vol = 0.01
            # Phase 2: The Crash (Feb 20 - Mar 23)
            elif day < date(2020, 3, 23):
                drift = -0.015 # Heavy daily drops
                vol = 0.04    # High volatility
            # Phase 3: The Recovery (Mar 23 onwards)
            else:
                drift = 0.003  # Strong bounce
                vol = 0.02
            
            rng = random.Random(day.toordinal())
            noise = rng.uniform(-vol, vol)
            
            price = max(10.0, price * (1 + drift + noise))
            close = round(price, 2)
            open_p = round(close * (1 + rng.uniform(-0.01, 0.01)), 2)
            high = round(max(open_p, close) * (1 + rng.uniform(0, vol/2)), 2)
            low = round(min(open_p, close) * (1 - rng.uniform(0, vol/2)), 2)
            volume = int(rng.uniform(5000000, 15000000))
            
            candles.append({
                "date": day.isoformat(),
                "open": open_p, "high": high, "low": low, "close": close, "volume": volume
            })
        day += timedelta(days=1)
    
    import os
    os.makedirs("data/prices", exist_ok=True)
    with open("data/prices/US_CRASH.csv", "w", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(candles)

    # Fundamentals: SPY usually has moderate earnings
    fundamentals = [
        {"period": "2020Q2", "eps_yoy": -20.0, "revenue_yoy": -10.0, "reported_at": "2020-07-25"},
        {"period": "2020Q1", "eps_yoy": 5.0, "revenue_yoy": 8.0, "reported_at": "2020-04-25"}
    ]
    with open("data/fundamentals/US_CRASH.json", "w") as f:
        json.dump(fundamentals, f)

if __name__ == "__main__":
    create_covid_crash_data()
    print("COVID Crash Fixture Created.")
