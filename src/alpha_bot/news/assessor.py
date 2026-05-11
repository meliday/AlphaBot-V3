"""LLM-based news assessment.

Converts free-form news text into a structured ``NewsAssessment`` that the
strategy analyzer consumes as an input signal. When no API key is configured
or the LLM call fails, a neutral assessment is returned so the rest of the
pipeline keeps working deterministically.
"""

from __future__ import annotations

import json
import logging
import os

from alpha_bot.config import load_dotenv
from alpha_bot.models import Catalyst, NewsAssessment

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """당신은 주식 뉴스를 분석하는 퀀트 애널리스트입니다.

주어진 종목의 최근 뉴스/카탈리스트를 읽고 아래 JSON 형식으로만 응답하세요.

{
  "sentiment": "positive" | "neutral" | "negative",
  "severity":  "low" | "medium" | "high",
  "earnings_caution": true | false,
  "score_adjustment": -3 ~ +3 (정수),
  "reasoning": "1~3문장 한국어 요약"
}

판단 기준:
- sentiment: 뉴스 전반의 톤
- severity:  주가에 미칠 영향의 강도
- earnings_caution: 실적 둔화/하향, 가이던스 컷, 소송/규제 리스크 등 펀더멘털
  악재가 명확하면 true. 단순 노이즈면 false.
- score_adjustment:
   +3 = 게임체인저급 호재(빅 계약, 어닝 서프라이즈, 신제품 메가히트)
   +1~+2 = 우호적 뉴스
    0  = 특별한 이벤트 없음/혼조
   -1~-2 = 부정적 뉴스
   -3 = 심각한 악재(가이던스 컷, 회계 이슈, 소송, 핵심 임원 사임, 대규모 리콜)
- reasoning: 판단 근거를 짧고 명확하게.

위 JSON 한 객체 외의 텍스트는 절대 출력하지 마세요."""


def assess_news(
    ticker: str,
    market: str,
    news_text: str,
    catalysts: list[Catalyst] | None = None,
) -> NewsAssessment:
    """Return an LLM-derived assessment of recent news for ``ticker``.

    Always returns a valid ``NewsAssessment``. On any failure (missing API
    key, network error, malformed response) a neutral assessment is
    returned and the reason is logged.
    """

    load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return neutral_assessment(
            "OPENAI_API_KEY가 없어 LLM 뉴스 분석을 생략했습니다.", news_text
        )

    try:
        from openai import OpenAI  # local import; openai is an optional dep
    except ImportError:
        return neutral_assessment(
            "openai 패키지가 설치되어 있지 않아 LLM 분석을 생략했습니다.", news_text
        )

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    catalysts = catalysts or []
    catalyst_block = (
        "\n".join(
            f"- [{c.source}] {c.headline}: {c.summary}" for c in catalysts
        )
        or "(로컬 카탈리스트 없음)"
    )
    user = (
        f"종목: {ticker} ({market})\n\n"
        f"[수집된 뉴스]\n{news_text or '(뉴스 없음)'}\n\n"
        f"[로컬 카탈리스트]\n{catalyst_block}\n\n"
        "위 정보를 바탕으로 JSON 객체 한 개만 출력하세요."
    )

    client = OpenAI(api_key=api_key)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    # Some newer reasoning models reject the temperature param entirely; try
    # with low temperature first, then fall back to model defaults.
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        if "temperature" in str(exc).lower():
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"},
                )
            except Exception as exc2:
                logger.warning("LLM news assessment failed for %s: %s", ticker, exc2)
                return neutral_assessment(f"LLM 호출 실패: {exc2}", news_text)
        else:
            logger.warning("LLM news assessment failed for %s: %s", ticker, exc)
            return neutral_assessment(f"LLM 호출 실패: {exc}", news_text)

    try:
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
    except (json.JSONDecodeError, AttributeError, IndexError) as exc:
        logger.warning("LLM response parse failed for %s: %s", ticker, exc)
        return neutral_assessment(f"LLM 응답 파싱 실패: {exc}", news_text)

    try:
        from alpha_bot.audit_log import log_llm
        log_llm(ticker, market, model, news_text, raw, data)
    except Exception:
        pass

    return _coerce_assessment(data, news_text, source=f"openai:{model}")


def neutral_assessment(reason: str, news_text: str = "") -> NewsAssessment:
    return NewsAssessment(
        sentiment="neutral",
        severity="low",
        earnings_caution=False,
        score_adjustment=0,
        reasoning=reason,
        raw_news=news_text,
        source="default",
    )


def _coerce_assessment(data: dict, news_text: str, source: str) -> NewsAssessment:
    sentiment = str(data.get("sentiment", "neutral")).lower()
    if sentiment not in {"positive", "neutral", "negative"}:
        sentiment = "neutral"
    severity = str(data.get("severity", "low")).lower()
    if severity not in {"low", "medium", "high"}:
        severity = "low"
    try:
        adjustment = int(data.get("score_adjustment", 0))
    except (TypeError, ValueError):
        adjustment = 0
    adjustment = max(-3, min(3, adjustment))
    return NewsAssessment(
        sentiment=sentiment,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        earnings_caution=bool(data.get("earnings_caution", False)),
        score_adjustment=adjustment,
        reasoning=str(data.get("reasoning", "")).strip(),
        raw_news=news_text,
        source=source,
    )
