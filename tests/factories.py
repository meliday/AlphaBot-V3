from __future__ import annotations

from alpha_bot.data import SyntheticDataProvider
from alpha_bot.models import Candle, Market


def demo_candles(ticker: str = "NVDA", market: Market = "US", lookback: int = 260) -> list[Candle]:
    return SyntheticDataProvider().get_candles(ticker, market, lookback)


def downtrend_candles() -> list[Candle]:
    candles = demo_candles()
    adjusted = []
    price = 300.0
    for candle in candles:
        price *= 0.996
        adjusted.append(
            Candle(
                date=candle.date,
                open=price * 1.01,
                high=price * 1.02,
                low=price * 0.98,
                close=price,
                volume=candle.volume,
            )
        )
    return adjusted
