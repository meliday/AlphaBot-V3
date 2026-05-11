import unittest

from alpha_bot.data import SyntheticDataProvider
from alpha_bot.models import NewsAssessment
from alpha_bot.news import neutral_assessment
from alpha_bot.strategy import StrategyAnalyzer
from tests.factories import demo_candles


def _provider():
    return SyntheticDataProvider()


def _analyze(news: NewsAssessment | None = None, min_score: int = 24):
    provider = _provider()
    return StrategyAnalyzer(min_score=min_score).analyze(
        "NVDA",
        "US",
        demo_candles(),
        provider.get_fundamentals("NVDA", "US"),
        provider.get_catalysts("NVDA", "US"),
        provider.get_market_context("NVDA", "US"),
        news_assessment=news,
    )


class NewsAssessmentTests(unittest.TestCase):
    def test_neutral_assessment_does_not_change_signal(self):
        baseline = _analyze(news=None)
        with_neutral = _analyze(news=neutral_assessment("test"))
        self.assertEqual(baseline.signal, with_neutral.signal)
        self.assertEqual(baseline.scoreboard.total, with_neutral.scoreboard.total)

    def test_severe_negative_news_forces_hold_off(self):
        bad = NewsAssessment(
            sentiment="negative",
            severity="high",
            earnings_caution=True,
            score_adjustment=-3,
            reasoning="가이던스 컷 + 회계 이슈",
            source="test",
        )
        report = _analyze(news=bad)
        self.assertEqual(report.signal, "Hold Off")
        self.assertTrue(report.earnings_caution)

    def test_positive_news_boosts_fundamentals_score(self):
        baseline = _analyze(news=None)
        boosted = _analyze(
            news=NewsAssessment(
                sentiment="positive",
                severity="medium",
                earnings_caution=False,
                score_adjustment=2,
                reasoning="대형 계약 체결",
                source="test",
            )
        )
        self.assertGreaterEqual(
            boosted.scoreboard.fundamentals,
            baseline.scoreboard.fundamentals,
        )
        # The bump should propagate to the total score (capped at 10 per sub-score).
        self.assertGreaterEqual(boosted.scoreboard.total, baseline.scoreboard.total)

    def test_news_assessment_attached_to_report(self):
        na = neutral_assessment("attach test")
        report = _analyze(news=na)
        self.assertIsNotNone(report.news_assessment)
        self.assertEqual(report.news_assessment.reasoning, "attach test")


if __name__ == "__main__":
    unittest.main()
