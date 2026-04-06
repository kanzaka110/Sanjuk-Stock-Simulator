"""
감성 분석 — 뉴스 텍스트를 수치 점수로 변환

Gemini를 활용하여 뉴스 헤드라인/본문의 감성을
-100(극도 부정) ~ +100(극도 긍정) 점수로 변환한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from config.settings import GEMINI_API_KEY

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentimentScore:
    """종목별 감성 점수."""

    ticker: str
    name: str
    score: int = 0  # -100 ~ +100
    label: str = "중립"  # 극도부정/부정/약부정/중립/약긍정/긍정/극도긍정
    summary: str = ""  # 핵심 감성 요약 (1줄)


@dataclass(frozen=True)
class MarketSentiment:
    """시장 전체 감성."""

    overall_score: int = 0
    overall_label: str = "중립"
    stock_scores: tuple[SentimentScore, ...] = ()
    fear_greed: str = "중립"  # 공포/불안/중립/탐욕/극도탐욕

    def to_text(self) -> str:
        lines = [
            f"【감성 분석】",
            f"  시장 전체: {self.overall_score:+d} [{self.overall_label}] | 공포탐욕: {self.fear_greed}",
        ]
        for s in self.stock_scores:
            lines.append(f"  {s.name}: {s.score:+d} [{s.label}] — {s.summary}")
        return "\n".join(lines)


def _score_to_label(score: int) -> str:
    if score <= -60:
        return "극도부정"
    if score <= -30:
        return "부정"
    if score <= -10:
        return "약부정"
    if score <= 10:
        return "중립"
    if score <= 30:
        return "약긍정"
    if score <= 60:
        return "긍정"
    return "극도긍정"


def _fear_greed_label(score: int) -> str:
    if score <= -40:
        return "극도공포"
    if score <= -15:
        return "공포"
    if score <= 15:
        return "중립"
    if score <= 40:
        return "탐욕"
    return "극도탐욕"


def analyze_sentiment(
    news_text: str,
    stock_names: list[str],
) -> MarketSentiment:
    """뉴스 텍스트에서 감성 점수 추출.

    Args:
        news_text: Gemini가 수집한 뉴스 전체 텍스트
        stock_names: 종목명 리스트 (감성을 추출할 대상)

    Returns:
        MarketSentiment (실패 시 기본값 반환)
    """
    if not GEMINI_API_KEY:
        return MarketSentiment()

    stocks_str = ", ".join(stock_names)
    prompt = f"""다음 뉴스/분석 텍스트를 읽고 감성 점수를 JSON으로 반환해.

점수 범위: -100(극도 부정) ~ +100(극도 긍정)

출력 형식 (순수 JSON, 코드블록 없이):
{{
  "overall": {{"score": 0, "summary": "시장 전체 한줄 요약"}},
  "stocks": [
    {{"name": "종목명", "score": 0, "summary": "해당 종목 감성 한줄 요약"}}
  ]
}}

분석 대상 종목: {stocks_str}

뉴스 텍스트:
{news_text[:8000]}"""

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=2000,
                response_mime_type="application/json",
            ),
        )
        raw = response.text.strip() if response.text else ""
        if not raw:
            log.warning("감성 분석: 빈 응답")
            return MarketSentiment()

        # JSON 파싱
        data: dict = {}
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip().lstrip("json").strip()
                try:
                    data = json.loads(part)
                    break
                except json.JSONDecodeError:
                    continue
        if not data:
            data = json.loads(raw)

        overall = data.get("overall", {})
        overall_score = int(overall.get("score", 0))

        stock_scores: list[SentimentScore] = []
        for item in data.get("stocks", []):
            s = int(item.get("score", 0))
            stock_scores.append(
                SentimentScore(
                    ticker="",
                    name=item.get("name", ""),
                    score=s,
                    label=_score_to_label(s),
                    summary=item.get("summary", ""),
                )
            )

        return MarketSentiment(
            overall_score=overall_score,
            overall_label=_score_to_label(overall_score),
            stock_scores=tuple(stock_scores),
            fear_greed=_fear_greed_label(overall_score),
        )
    except Exception as e:
        log.warning(f"감성 분석 실패: {e}")
        return MarketSentiment()
