import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from alpha_bot.models import NewsAssessment
from alpha_bot.news import cache as news_cache


def _llm_assessment(reasoning: str = "test") -> NewsAssessment:
    return NewsAssessment(
        sentiment="positive",
        severity="medium",
        earnings_caution=False,
        score_adjustment=2,
        reasoning=reasoning,
        raw_news="raw",
        source="openai:gpt-4o-mini",
    )


class NewsCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._prev_path = os.environ.get("NEWS_CACHE_PATH")
        self._prev_ttl = os.environ.get("NEWS_CACHE_TTL_SECONDS")
        os.environ["NEWS_CACHE_PATH"] = str(Path(self._tmp.name) / "news_cache.json")
        os.environ["NEWS_CACHE_TTL_SECONDS"] = "3600"

    def tearDown(self):
        self._tmp.cleanup()
        for key, val in (("NEWS_CACHE_PATH", self._prev_path), ("NEWS_CACHE_TTL_SECONDS", self._prev_ttl)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_miss_when_empty(self):
        self.assertIsNone(news_cache.load("NVDA", "US"))

    def test_save_then_load_within_ttl(self):
        original = _llm_assessment("first")
        news_cache.save("NVDA", "US", original)
        loaded = news_cache.load("NVDA", "US")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.reasoning, "first")
        self.assertEqual(loaded.score_adjustment, 2)

    def test_expired_entry_returns_none(self):
        news_cache.save("NVDA", "US", _llm_assessment())
        path = news_cache.cache_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        data["US:NVDA"]["cached_at"] = stale
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(news_cache.load("NVDA", "US"))

    def test_neutral_assessment_not_cached(self):
        neutral = NewsAssessment(
            sentiment="neutral",
            severity="low",
            earnings_caution=False,
            score_adjustment=0,
            reasoning="no key",
            source="default",
        )
        news_cache.save("NVDA", "US", neutral)
        self.assertIsNone(news_cache.load("NVDA", "US"))

    def test_ttl_zero_disables_cache(self):
        os.environ["NEWS_CACHE_TTL_SECONDS"] = "0"
        news_cache.save("NVDA", "US", _llm_assessment())
        self.assertIsNone(news_cache.load("NVDA", "US"))

    def test_keys_are_namespaced_by_market(self):
        news_cache.save("005930", "KR", _llm_assessment("kr"))
        news_cache.save("005930", "US", _llm_assessment("us"))
        self.assertEqual(news_cache.load("005930", "KR").reasoning, "kr")
        self.assertEqual(news_cache.load("005930", "US").reasoning, "us")


if __name__ == "__main__":
    unittest.main()
