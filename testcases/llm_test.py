from alpha_bot.models import NewsAssessment
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.backtest import Backtester
from alpha_bot.data.providers import FixtureDataProvider
from pathlib import Path

def run_llm_boosted_test():
    # 1. 로컬 데이터 로드
    provider = FixtureDataProvider(Path("data"))
    ticker, market = "000660", "KR"
    candles = provider.get_candles(ticker, market, lookback=500)
    fundamentals = provider.get_fundamentals(ticker, market)
    context = provider.get_market_context(ticker, market)
    
    # 2. LLM 가산점을 모사하기 위해 analyzer를 래핑하거나 조정
    # 여기서는 간단히 뉴스 보정값이 항상 +3인 상태를 가정하여 analyze_ticker 역할을 수행
    class BoostedAnalyzer(StrategyAnalyzer):
        def analyze(self, *args, **kwargs):
            # 항상 +3점의 긍정적 뉴스 평가를 주입
            kwargs['news_assessment'] = NewsAssessment(
                sentiment="positive", severity="high", earnings_caution=False,
                score_adjustment=3, reasoning="HBM4 메가 호재 반영 (가상)"
            )
            return super().analyze(*args, **kwargs)

    analyzer = BoostedAnalyzer(min_score=24, min_rr=1.5)
    tester = Backtester(analyzer)
    
    print(f"=== 🚀 SK하이닉스 [LLM 가산점 +3 반영] 테스트 ===")
    res = tester.run(ticker, market, candles, fundamentals, [], context)
    
    print(f"결과: 매매 {len(res.trades)}건, 수익률 {res.total_return_pct:.1f}%, MDD {res.max_drawdown_pct:.1f}%")
    for t in res.trades:
        print(f"  {t.entry_date} -> {t.exit_date} [{t.outcome}] 리턴: {t.return_pct:.1f}% (진입가: {t.entry:,.0f})")

if __name__ == "__main__":
    run_llm_boosted_test()
