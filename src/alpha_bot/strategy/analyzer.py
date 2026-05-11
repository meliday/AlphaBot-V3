from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from alpha_bot.data.providers import compound_return
from alpha_bot.errors import StrategyError
from alpha_bot.models import (
    AnalysisReport,
    Candle,
    Catalyst,
    FundamentalsQuarter,
    IndicatorSnapshot,
    Market,
    MarketContext,
    NewsAssessment,
    Scoreboard,
    Signal,
    TechnicalBreakdown,
)
from alpha_bot.risk import build_trade_plan
from alpha_bot.strategy.indicators import (
    detect_vcp,
    latest_bollinger,
    latest_rsi,
    latest_sma,
    volume_accumulation_summary,
)

logger = logging.getLogger(__name__)


# ── Strategy Parameters ──────────────────────────────────────────────
# All "magic numbers" that were previously hardcoded throughout the
# scoring logic are gathered here for transparency and easy tuning.

@dataclass(frozen=True)
class StrategyParams:
    """Tuneable thresholds used by :class:`StrategyAnalyzer`."""

    # Fundamentals scoring
    eps_yoy_strong: float = 25.0      # EPS YoY ≥ this → +4 points
    eps_yoy_good: float = 15.0        # EPS YoY ≥ this → +3 points
    revenue_yoy_strong: float = 20.0  # Revenue YoY ≥ this → +3 points
    revenue_yoy_good: float = 10.0    # Revenue YoY ≥ this → +2 points

    # Trend scoring
    near_high_pct: float = 0.90       # close ≥ 52w-high × this → +2 points

    # Momentum scoring
    rsi_bullish_low: float = 50.0     # RSI in [low, high] → +2 points
    rsi_bullish_high: float = 75.0    # RSI above this → +1 (extended)
    # Bollinger Band width = (upper - lower) / mid, with 2-σ bands.
    # Typical Bollinger Squeeze ("low-volatility coil") values per Bollinger's
    # own thesis are 0.05–0.10 of price; values above ~0.15 are not a squeeze.
    # Previous defaults of 0.18 / 0.28 awarded +2/+1 to virtually all stocks
    # and inflated momentum scores by a near-constant +1 to +2.
    bollinger_tight: float = 0.08     # Bollinger width < this → +2 (true squeeze)
    bollinger_moderate: float = 0.14  # Bollinger width < this → +1 (compressed)
    relative_strength_threshold: float = 5.0  # outperformance > this → +1

    # Signal decision
    strong_buy_score: int = 27        # score ≥ this AND rr ≥ strong_buy_rr
    strong_buy_rr: float = 3.0
    # Score-adjusted R/R floor: high-conviction setups tolerate tighter risk/reward
    # because earnings power and market structure compress the achievable R/R
    # (e.g. SK Hynix near 52w-high with HBM cycle tailwind).
    high_conviction_score: int = 27   # score ≥ this → relax min_rr to high_conviction_min_rr
    high_conviction_min_rr: float = 1.2
    highest_conviction_score: int = 29  # score ≥ this → relax further
    highest_conviction_min_rr: float = 1.0


class StrategyAnalyzer:
    def __init__(
        self,
        min_score: int = 24,
        min_rr: float = 1.5,
        params: StrategyParams | None = None,
    ):
        self.min_score = min_score
        self.min_rr = min_rr
        self.params = params or StrategyParams()

    def analyze(
        self,
        ticker: str,
        market: Market,
        candles: list[Candle],
        fundamentals: list[FundamentalsQuarter],
        catalysts: list[Catalyst],
        context: MarketContext,
        company_name: str | None = None,
        language: str = "ko",
        news_assessment: NewsAssessment | None = None,
    ) -> AnalysisReport:
        if len(candles) < 20:
            raise StrategyError(
                f"데이터가 너무 적습니다 ({len(candles)}개 캔들). 최소 20개가 필요합니다."
            )

        n = len(candles)
        # Track data completeness so scoring/signal logic can adjust
        has_sma200 = n >= 200
        has_sma50  = n >= 50

        logger.info("Analyzing %s:%s (%d candles, sma200=%s)", market, ticker, n, has_sma200)

        closes = [candle.close for candle in candles]
        close = closes[-1]
        sma50 = latest_sma(closes, 50)    # partial if n < 50
        sma200 = latest_sma(closes, 200)  # partial if n < 200
        rsi14 = latest_rsi(closes, 14)    # 50.0 neutral if n < 15
        boll_mid, boll_upper, boll_lower, boll_width = latest_bollinger(closes)
        support_20d = min(candle.low for candle in candles[-20:])
        resistance_60d = max(candle.high for candle in candles[-60:])
        volume_score, volume_summary = volume_accumulation_summary(candles)
        vcp_pattern, vcp_score, vcp_details = detect_vcp(candles)
        trade_plan = build_trade_plan(candles, sma50, self.min_rr)

        fundamentals_score, fundamentals_note, earnings_caution = self._score_fundamentals(fundamentals, catalysts)
        trend_score, trend_note = self._score_trend(close, sma50, sma200, candles, has_sma200, has_sma50)
        momentum_score, momentum_note = self._score_momentum(
            rsi14, boll_width, vcp_score, volume_score, context, candles
        )

        if news_assessment is not None:
            adj = news_assessment.score_adjustment
            if adj != 0:
                fundamentals_score = max(0, min(10, fundamentals_score + adj))
                fundamentals_note += f"; LLM 뉴스 조정 {adj:+d}"
            if news_assessment.earnings_caution:
                earnings_caution = True

        scoreboard = Scoreboard(
            fundamentals=fundamentals_score,
            fundamentals_note=fundamentals_note,
            technical_trend=trend_score,
            technical_trend_note=trend_note,
            momentum_flow=momentum_score,
            momentum_flow_note=momentum_note,
        )
        indicators = IndicatorSnapshot(
            close=close,
            sma50=sma50,
            sma200=sma200,
            rsi14=rsi14,
            bollinger_mid=boll_mid,
            bollinger_upper=boll_upper,
            bollinger_lower=boll_lower,
            bollinger_width=boll_width,
            resistance_60d=resistance_60d,
            support_20d=support_20d,
            volume_summary=volume_summary,
            vcp_pattern=vcp_pattern,
            vcp_score=vcp_score,
            vcp_details=vcp_details,
        )
        lang = "ko" if language.lower().startswith("ko") else "en"
        technical = self._technical_breakdown(close, sma50, sma200, indicators, context, candles, lang)
        signal, reason = self._final_signal(
            close, sma200, scoreboard, trade_plan.rr_ratio, lang, news_assessment, market,
            has_sma200=has_sma200,
        )
        catalyst_summary = self._catalyst_summary(catalysts, lang)

        logger.info(
            "%s:%s → %s (score=%d/30, rr=%.2f:1)",
            market, ticker, signal, scoreboard.total, trade_plan.rr_ratio,
        )
        try:
            from alpha_bot.audit_log import log_query
            na = news_assessment
            log_query(
                ticker=ticker,
                market=market,
                signal=signal,
                score=scoreboard.total,
                rr=trade_plan.rr_ratio,
                reason=reason,
                news_sentiment=na.sentiment if na else None,
                news_adjustment=na.score_adjustment if na else None,
            )
        except Exception:
            pass

        return AnalysisReport(
            ticker=ticker.upper(),
            company_name=company_name or ticker.upper(),
            market=market,
            signal=signal,
            reason=reason,
            scoreboard=scoreboard,
            indicators=indicators,
            trade_plan=trade_plan,
            technical=technical,
            earnings_caution=earnings_caution,
            catalyst_summary=catalyst_summary,
            as_of=candles[-1].date,
            language="ko" if language.lower().startswith("ko") else "en",
            news_assessment=news_assessment,
        )

    def _score_fundamentals(
        self, fundamentals: list[FundamentalsQuarter], catalysts: list[Catalyst]
    ) -> tuple[int, str, bool]:
        p = self.params
        # Honest "unknown" — if we have no fundamentals data, we cannot confirm
        # earnings strength. Catalysts alone don't substitute for actual numbers.
        if not fundamentals:
            note = "펀더멘털 데이터 없음 — 평가 불가 (0점 처리)"
            return (0, note, False)
        latest = fundamentals[0]
        previous = fundamentals[1] if len(fundamentals) > 1 else None
        score = 0
        eps = latest.eps_yoy
        revenue = latest.revenue_yoy
        earnings_caution = (eps is not None and eps < 0) or (revenue is not None and revenue < 0)

        if eps is not None:
            score += 4 if eps >= p.eps_yoy_strong else 3 if eps >= p.eps_yoy_good else 1 if eps > 0 else 0
        if revenue is not None:
            score += 3 if revenue >= p.revenue_yoy_strong else 2 if revenue >= p.revenue_yoy_good else 1 if revenue > 0 else 0
        # Q-over-Q acceleration: reward sequential improvement in EPS YoY.
        # If we have 3+ quarters and growth is monotonically accelerating
        # ("C" in CANSLIM is most powerful when sustained), award +2 instead
        # of the previous flat +1. A single Q ≥ prior Q gets +1 as before.
        prev_prev = fundamentals[2] if len(fundamentals) > 2 else None
        if previous and previous.eps_yoy is not None and eps is not None:
            if eps >= previous.eps_yoy:
                if (
                    prev_prev
                    and prev_prev.eps_yoy is not None
                    and previous.eps_yoy >= prev_prev.eps_yoy
                ):
                    score += 2  # two-quarter sustained acceleration
                else:
                    score += 1
        if latest.eps_surprise is not None and latest.eps_surprise > 0:
            score += 1
        # Catalyst point only counts if backed by actual EPS/revenue data (avoids
        # rewarding "had a press release but no numbers" cases).
        if catalysts and eps is not None and revenue is not None:
            score += 1

        eps_text = "n/a" if eps is None else f"{eps:.1f}% EPS YoY"
        rev_text = "n/a" if revenue is None else f"{revenue:.1f}% revenue YoY"
        if latest.etf_proxy:
            # period encodes the ETF ticker and sampled holdings: "ETF:SOXL→[NVDA, AMD, ...]"
            note = f"[ETF 프록시] 상위 보유 종목 가중평균 — {eps_text}, {rev_text} ({latest.period})"
        else:
            note = f"latest quarter: {eps_text}, {rev_text}; {len(catalysts)} catalyst(s)"
        return min(score, 10), note, earnings_caution

    def _score_trend(
        self,
        close: float,
        sma50: float,
        sma200: float,
        candles: list[Candle],
        has_sma200: bool = True,
        has_sma50: bool = True,
    ) -> tuple[int, str]:
        p = self.params
        score = 0
        n = len(candles)
        notes = [f"close {close:.2f}"]

        if has_sma200:
            if close > sma200:
                score += 4
            if has_sma50 and sma50 > sma200:
                score += 2
            notes.append(f"SMA200 {sma200:.2f}")
        else:
            # Partial SMA — only use for close vs SMA comparison, halved weight
            if close > sma200:
                score += 2  # half credit: approximate trend
            notes.append(f"SMA{n}d {sma200:.2f} (부족 — {n}일 데이터)")

        if has_sma50:
            if close > sma50:
                score += 2
            notes.append(f"SMA50 {sma50:.2f}")
        else:
            if close > sma50:
                score += 1
            notes.append(f"SMA{min(50,n)}d {sma50:.2f} (부족)")

        high_52w = max(candle.high for candle in candles[-252:])
        if close >= high_52w * p.near_high_pct:
            score += 2

        return min(score, 10), "; ".join(notes)

    def _score_momentum(
        self,
        rsi14: float,
        boll_width: float,
        vcp_score: int,
        volume_score: int,
        context: MarketContext,
        candles: list[Candle],
    ) -> tuple[int, str]:
        p = self.params
        score = 0
        if p.rsi_bullish_low <= rsi14 <= p.rsi_bullish_high:
            score += 2
        elif rsi14 > p.rsi_bullish_high:
            score += 1
        if boll_width < p.bollinger_tight:
            score += 2
        elif boll_width < p.bollinger_moderate:
            score += 1
        score += min(3, round(vcp_score / 3))
        score += volume_score

        relative_return = self._relative_strength(context, candles)
        if relative_return is not None and relative_return > p.relative_strength_threshold:
            score += 1

        note = f"RSI {rsi14:.1f}, Bollinger width {boll_width * 100:.1f}%, VCP {vcp_score}/10"
        return min(score, 10), note

    def _relative_strength(self, context: MarketContext, candles: list[Candle]) -> float | None:
        """Return stock return minus benchmark return over ~3 months.

        We deliberately refuse to fall back to absolute return: in a strong
        bull market every name shows positive absolute return, which would
        award a free momentum point to all candidates. Returning None when
        the benchmark is missing forces the scorer to skip the relative-
        strength bonus rather than inflate it.
        """
        if context.stock_return_3m is not None and context.benchmark_return_3m is not None:
            return context.stock_return_3m - context.benchmark_return_3m
        # Try to derive the benchmark return from the market regime cache
        try:
            from alpha_bot.market_regime import get_regime
            stock_return = compound_return(candles, 63)
            if stock_return is None:
                return None
            # Note: this is a best-effort estimate. Without a per-market benchmark
            # candle series we cannot compute a precise relative return, so we
            # require an explicit benchmark value above and otherwise abstain.
        except Exception:
            pass
        return None

    def _technical_breakdown(
        self,
        close: float,
        sma50: float,
        sma200: float,
        indicators: IndicatorSnapshot,
        context: MarketContext,
        candles: list[Candle],
        language: str = "ko",
    ) -> TechnicalBreakdown:
        if language == "ko":
            above_200 = "위" if close > sma200 else "아래"
            above_50 = "위" if close > sma50 else "아래"
            trend = f"현재가가 200일선 {above_200}, 50일선 {above_50}에 위치합니다."
            if close > sma200 and sma50 > sma200:
                trend += " 이동평균선 정배열 상태입니다."
        else:
            above_200 = "above" if close > sma200 else "below"
            above_50 = "above" if close > sma50 else "below"
            trend = f"Price is {above_200} the 200-day SMA and {above_50} the 50-day SMA."
            if close > sma200 and sma50 > sma200:
                trend += " Moving-average alignment is constructive."
        pattern = f"{indicators.vcp_pattern}; {indicators.vcp_details}."
        volume = indicators.volume_summary
        risk_parts = []
        if language == "ko":
            if close < sma200:
                risk_parts.append("마스터 추세 필터 실패 (200일선 하회)")
            if indicators.rsi14 > 75:
                risk_parts.append("RSI 과열 구간")
            if close < sma50:
                risk_parts.append("현재가 50일선 지지 이탈")
            risk_factors = ", ".join(risk_parts) if risk_parts else "돌파 실패 및 50일선 이탈 여부 주시 필요"
        else:
            if close < sma200:
                risk_parts.append("master trend filter failed")
            if indicators.rsi14 > 75:
                risk_parts.append("RSI extended")
            if close < sma50:
                risk_parts.append("price below 50-day support")
            risk_factors = ", ".join(risk_parts) if risk_parts else "watch for failed breakout and 50-day SMA loss"

        rel = self._relative_strength(context, candles)
        if language == "ko":
            if rel is None:
                relative = f"섹터 테마: {context.sector_theme}; 상대강도 데이터 미확보."
            else:
                relative = f"벤치마크 대비 약 3개월간 {rel:.1f}% 초과 수익; 테마: {context.sector_theme}"
            if context.foreign_flow != "unknown" or context.institutional_flow != "unknown":
                relative += f"; 외국인 수급 {context.foreign_flow}, 기관 수급 {context.institutional_flow}"
        else:
            if rel is None:
                relative = f"Sector theme: {context.sector_theme}; relative strength data unavailable."
            else:
                relative = f"outperforming benchmark by {rel:.1f}% over roughly 3 months; theme: {context.sector_theme}"
            if context.foreign_flow != "unknown" or context.institutional_flow != "unknown":
                relative += f"; foreign flow {context.foreign_flow}, institutional flow {context.institutional_flow}"
        return TechnicalBreakdown(trend, pattern, volume, risk_factors, relative)

    def _final_signal(
        self,
        close: float,
        sma200: float,
        scoreboard: Scoreboard,
        rr_ratio: float,
        language: str = "ko",
        news: NewsAssessment | None = None,
        market: Market | None = None,
        has_sma200: bool = True,
    ) -> tuple[Signal, str]:
        p = self.params
        # LLM veto: severe negative news blocks new entries regardless of score.
        if news and news.severity == "high" and news.sentiment == "negative":
            if language == "ko":
                return "Hold Off", f"LLM이 심각한 악재 감지: {news.reasoning or '뉴스 톤 매우 부정적'}"
            return "Hold Off", f"LLM detected severe negative news: {news.reasoning or 'tone strongly negative'}"
        # CANSLIM "M" veto: broad index below SMA200 blocks new buys.
        if market:
            try:
                from alpha_bot.market_regime import get_regime
                regime = get_regime(market)
                if not regime.is_bullish:
                    if language == "ko":
                        return "Hold Off", f"시장 레짐 약세: {regime.reason}"
                    return "Hold Off", f"Market regime bearish: {regime.reason}"
            except Exception:
                pass  # fail-open
        # Score-adjusted R/R floor: high-conviction setups get a relaxed minimum.
        # A stock like SK Hynix near resistance with exceptional fundamentals
        # structurally compresses achievable R/R — penalising it equally with a
        # low-conviction name distorts the signal more than it protects capital.
        effective_min_rr = self.min_rr
        if scoreboard.total >= p.highest_conviction_score:
            effective_min_rr = min(self.min_rr, p.highest_conviction_min_rr)
        elif scoreboard.total >= p.high_conviction_score:
            effective_min_rr = min(self.min_rr, p.high_conviction_min_rr)

        if language == "ko":
            if has_sma200 and close < sma200:
                return "Hold Off", "현재가가 200일선 아래에 있어 마스터 추세 필터에 의해 신규 매수가 차단됩니다."
            if not has_sma200 and close < sma200:
                return "Wait", f"단기 이동평균({round(sma200,2)}) 아래 — 200일 데이터 부족으로 참고용 (데이터 확보 후 재분석 권장)"
            if rr_ratio < effective_min_rr:
                if effective_min_rr < self.min_rr:
                    return "Wait", (
                        f"1차 목표 R/R이 {rr_ratio:.2f}:1로, 고확신 셋업 완화 기준 "
                        f"{effective_min_rr:.1f}:1에도 미달합니다. (기본 기준 {self.min_rr:.1f}:1)"
                    )
                return "Wait", f"1차 목표 R/R이 {rr_ratio:.2f}:1로, 최소 기준 {self.min_rr:.1f}:1에 미달합니다."
            if scoreboard.total < self.min_score:
                return "Wait", f"종합 점수가 {scoreboard.total}/30점으로, 매수 기준 {self.min_score}/30에 미달합니다."
            if scoreboard.total >= p.strong_buy_score and rr_ratio >= p.strong_buy_rr:
                return "Strong Buy", "추세·점수·R/R이 모두 정렬된 고품질 셋업입니다."
            if effective_min_rr < self.min_rr:
                return "Buy", (
                    f"고확신 셋업 (점수 {scoreboard.total}/30): R/R {rr_ratio:.2f}:1이 완화 기준 "
                    f"{effective_min_rr:.1f}:1을 충족합니다."
                )
            return "Buy", "추세 필터, 퀀트 점수, R/R 모두 v1 진입 조건을 충족합니다."
        else:
            if has_sma200 and close < sma200:
                return "Hold Off", "Price is below the 200-day SMA, so the master trend filter blocks new buys."
            if not has_sma200 and close < sma200:
                return "Wait", f"Below short-term SMA ({round(sma200,2)}) — insufficient history for 200-day filter; re-analyze when more data is available."
            if rr_ratio < effective_min_rr:
                if effective_min_rr < self.min_rr:
                    return "Wait", (
                        f"Target-1 R/R is {rr_ratio:.2f}:1, below the high-conviction relaxed floor "
                        f"of {effective_min_rr:.1f}:1 (base min {self.min_rr:.1f}:1)."
                    )
                return "Wait", f"Target-1 R/R is {rr_ratio:.2f}:1, below the {self.min_rr:.1f}:1 minimum."
            if scoreboard.total < self.min_score:
                return "Wait", f"Total score is {scoreboard.total}/30, below the {self.min_score}/30 buy threshold."
            if scoreboard.total >= p.strong_buy_score and rr_ratio >= p.strong_buy_rr:
                return "Strong Buy", "Trend, score, and R/R are all aligned for a high-quality setup."
            if effective_min_rr < self.min_rr:
                return "Buy", (
                    f"High-conviction setup (score {scoreboard.total}/30): R/R {rr_ratio:.2f}:1 "
                    f"meets the relaxed {effective_min_rr:.1f}:1 floor."
                )
            return "Buy", "Trend filter, quant score, and R/R all meet the v1 entry rules."

    def _catalyst_summary(self, catalysts: list[Catalyst], language: str = "ko") -> str:
        if not catalysts:
            return "최근 확인된 카탈리스트 없음." if language == "ko" else "No recent verified catalyst in local cache."
        latest = catalysts[0]
        return f"{latest.headline}: {latest.summary}"
