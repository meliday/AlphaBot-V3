from __future__ import annotations

from alpha_bot.models import AnalysisReport, Market, NewsAssessment


def render_report(report: AnalysisReport) -> str:
    return _render_ko(report) if report.language == "ko" else _render_en(report)


def _render_news_section(news: NewsAssessment | None, language: str) -> str:
    if news is None:
        return ""
    sentiment_emoji = {"positive": "🟢", "neutral": "⚪", "negative": "🔴"}[news.sentiment]
    if language == "ko":
        title = "**6. LLM 뉴스 평가**"
        adj = f"{news.score_adjustment:+d}"
        body = (
            f"- 톤: {sentiment_emoji} {news.sentiment} / 강도: {news.severity}\n"
            f"- 펀더멘털 점수 조정: {adj} (실적 주의: {'예' if news.earnings_caution else '아니오'})\n"
            f"- 근거: {news.reasoning or '(없음)'}\n"
            f"- 출처: {news.source}"
        )
    else:
        title = "**6. LLM News Assessment**"
        adj = f"{news.score_adjustment:+d}"
        body = (
            f"- Tone: {sentiment_emoji} {news.sentiment} / Severity: {news.severity}\n"
            f"- Fundamentals score adjustment: {adj} (earnings caution: {news.earnings_caution})\n"
            f"- Reasoning: {news.reasoning or '(none)'}\n"
            f"- Source: {news.source}"
        )
    return f"\n\n{title}\n{body}"


def _money(value: float, market: Market) -> str:
    if market == "KR":
        return f"₩{value:,.0f}"
    return f"${value:,.2f}"


def _signal_emoji(signal: str) -> str:
    return {
        "Strong Buy": "🚨 Strong Buy / 강력 매수",
        "Buy": "✅ Buy / 매수",
        "Wait": "⚠️ Wait / 관망",
        "Hold Off": "⛔ Hold Off / 매수 보류",
        "Sell": "🔻 Sell / 매도",
    }[signal]


def _render_en(report: AnalysisReport) -> str:
    plan = report.trade_plan
    alternate = ""
    if plan.alternate_entry is not None and plan.alternate_rr_ratio is not None:
        alternate = (
            f"\n- Pullback comparison: {_money(plan.alternate_entry, report.market)} entry "
            f"would improve Target-1 R/R to {plan.alternate_rr_ratio:.1f} : 1"
        )
    caution = "\n[⚠️ Earnings Caution]\n" if report.earnings_caution else ""
    return f"""{caution}### 📊 [{report.ticker}: {report.company_name}] Deep Dive Report

**1. Final Signal**
{_signal_emoji(report.signal)}
> **{report.reason}**

**2. Quant Scoreboard (each /10)**
- Fundamentals: {report.scoreboard.fundamentals}/10 — {report.scoreboard.fundamentals_note}
- Technical Trend: {report.scoreboard.technical_trend}/10 — {report.scoreboard.technical_trend_note}
- Momentum & Flow: {report.scoreboard.momentum_flow}/10 — {report.scoreboard.momentum_flow_note}
- **Total: {report.scoreboard.total}/30** (entry recommended at 24+ / 30, or 80%+. Below this threshold, the final signal cannot be Strong Buy or Buy.)

**3. Technical Deep Dive**
- Trend: {report.technical.trend}
- Pattern: {report.technical.pattern}
- Volume: {report.technical.volume}
- Risk Factors: {report.technical.risk_factors}
- Relative Strength: {report.technical.relative_strength}

**4. Sniper Trading Plan**
- 🎯 Buy Zone: {_money(plan.entry_low, report.market)} – {_money(plan.entry_high, report.market)}
- 🛡️ Stop-loss: {_money(plan.stop_loss, report.market)} ({plan.stop_pct:.1f}% from entry)
- 🚀 Target 1: {_money(plan.target1, report.market)} (+{plan.target1_pct:.1f}%)
- 💰 Target 2: {_money(plan.target2, report.market)} (+{plan.target2_pct:.1f}%)
- ⚖️ R/R Ratio: {plan.rr_ratio:.1f} : 1{alternate}

**5. Analyst's Insight**
The current setup reads as {report.indicators.vcp_pattern} under the O'Neil/Minervini lens. Catalyst check: {report.catalyst_summary} The single most important watch item is whether price holds the 50-day SMA at {_money(report.indicators.sma50, report.market)} while volume remains constructive.{_render_news_section(report.news_assessment, "en")}

---
*This report is a structured analytical framework, not personalized investment advice. Markets carry risk; you are responsible for your own decisions.*
"""


def _render_ko(report: AnalysisReport) -> str:
    plan = report.trade_plan
    alternate = ""
    if plan.alternate_entry is not None and plan.alternate_rr_ratio is not None:
        alternate = (
            f"\n- 눌림목 비교: {_money(plan.alternate_entry, report.market)} 진입 시 "
            f"1차 목표 R/R은 {plan.alternate_rr_ratio:.1f} : 1"
        )
    caution = "\n[⚠️ 실적 주의]\n" if report.earnings_caution else ""
    return f"""{caution}### 📊 [{report.ticker}: {report.company_name}] 딥 다이브 리포트

**1. 최종 판결**
{_signal_emoji(report.signal)}
> **{report.reason}**

**2. 퀀트 스코어보드 (각 /10)**
- 펀더멘털: {report.scoreboard.fundamentals}/10 — {report.scoreboard.fundamentals_note}
- 기술적 추세: {report.scoreboard.technical_trend}/10 — {report.scoreboard.technical_trend_note}
- 수급·모멘텀: {report.scoreboard.momentum_flow}/10 — {report.scoreboard.momentum_flow_note}
- **종합: {report.scoreboard.total}/30** (24점 이상 또는 80% 이상일 때 진입 후보. 이 기준 미달이면 최종 판결은 강력 매수/매수가 될 수 없습니다.)

**3. 차트 정밀 분석**
- 추세: {report.technical.trend}
- 패턴: {report.technical.pattern}
- 수급: {report.technical.volume}
- 위험 요소: {report.technical.risk_factors}
- 상대강도: {report.technical.relative_strength}

**4. 스나이퍼 트레이딩 전략**
- 🎯 진입 추천가: {_money(plan.entry_low, report.market)} – {_money(plan.entry_high, report.market)}
- 🛡️ 칼손절가: {_money(plan.stop_loss, report.market)} ({plan.stop_pct:.1f}% from entry)
- 🚀 1차 목표가: {_money(plan.target1, report.market)} (+{plan.target1_pct:.1f}%)
- 💰 2차 목표가: {_money(plan.target2, report.market)} (+{plan.target2_pct:.1f}%)
- ⚖️ R/R Ratio: {plan.rr_ratio:.1f} : 1{alternate}

**5. 애널리스트 코멘트**
오닐/미너비니 관점에서 현재 셋업은 {report.indicators.vcp_pattern}로 분류됩니다. Catalyst 점검: {report.catalyst_summary} 지금 가장 중요한 관찰 포인트는 가격이 50일선 {_money(report.indicators.sma50, report.market)}을 지키면서 거래량이 우호적으로 유지되는지입니다.{_render_news_section(report.news_assessment, "ko")}

---
*본 리포트는 분석 프레임워크에 따른 구조화된 분석이며, 개인 맞춤 투자 자문이 아닙니다. 투자의 책임은 본인에게 있습니다.*
"""
