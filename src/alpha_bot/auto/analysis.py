"""One-shot analysis with optional LLM news + provider/broker factories.

Provides the ``analyze_ticker`` function used by both the CLI and the
auto-pilot, plus factory helpers for constructing data providers and
broker instances from configuration strings.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alpha_bot.broker import KisBroker, MockBroker
from alpha_bot.broker.base import Broker
from alpha_bot.data import (
    DataProvider,
    FixtureDataProvider,
    KisPriceDataProvider,
    SyntheticDataProvider,
)
from alpha_bot.models import (
    AnalysisReport,
    Market,
    NewsAssessment,
)
from alpha_bot.news import assess_news, cache as news_cache, neutral_assessment
from alpha_bot.strategy import StrategyAnalyzer

logger = logging.getLogger(__name__)


# ── Provider / broker factories ──────────────────────────────────────


def make_provider(source: str, data_dir: Path) -> DataProvider:
    if source == "demo":
        return SyntheticDataProvider()
    if source == "kis":
        return KisPriceDataProvider(FixtureDataProvider(data_dir))
    return FixtureDataProvider(data_dir)


def make_broker(name: str) -> Broker:
    return KisBroker() if name == "kis" else MockBroker()


# ── One-shot analysis with optional LLM news ─────────────────────────


def analyze_ticker(
    analyzer: StrategyAnalyzer,
    provider: DataProvider,
    ticker: str,
    market: Market,
    company: str | None = None,
    language: str = "ko",
    use_llm: bool = True,
) -> AnalysisReport:
    candles = provider.get_candles(ticker, market)
    fundamentals = provider.get_fundamentals(ticker, market)
    catalysts = provider.get_catalysts(ticker, market)
    context = provider.get_market_context(ticker, market)

    assessment: NewsAssessment | None = None
    if use_llm:
        assessment = _gather_news_assessment(ticker, market, catalysts)

    return analyzer.analyze(
        ticker,
        market,
        candles,
        fundamentals,
        catalysts,
        context,
        company_name=company,
        language=language,
        news_assessment=assessment,
    )


def _gather_news_assessment(ticker: str, market: Market, catalysts) -> NewsAssessment:
    cached = news_cache.load(ticker, market)
    if cached is not None:
        logger.info("News assessment cache hit for %s (%s)", ticker, market)
        return cached
    try:
        from alpha_bot.data.scraper import fetch_news
    except ImportError as exc:
        logger.warning("News scraper unavailable (%s); skipping LLM assessment", exc)
        return neutral_assessment("뉴스 수집 모듈 의존성 없음", "")
    try:
        news_text = fetch_news(ticker, market)
    except Exception as exc:
        logger.warning("News fetch failed for %s: %s", ticker, exc)
        return neutral_assessment(f"뉴스 수집 실패: {exc}", "")
    assessment = assess_news(ticker, market, news_text, catalysts)
    news_cache.save(ticker, market, assessment)
    return assessment
