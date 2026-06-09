"""
감성 분석 -- 뉴스 텍스트를 수치 점수로 변환

Gemini response_schema로 구조화된 JSON 출력을 강제하여
파싱 실패를 방지한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from config.settings import GEMINI_API_KEY

log = logging.getLogger(__name__)

# Gemini response_schema 정의
_SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "overall": {
            "type": "object",
            "properties": {
                "score": {"type": "integer"},
                "summary": {"type": "string"},
            },
            "required": ["score", "summary"],
        },
        "stocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "score": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "required": ["name", "score", "summary"],
            },
        },
    },
    "required": ["overall", "stocks"],
}


@dataclass(frozen=True)
class SentimentScore:
    """종목별 감성 점수."""

    ticker: str
    name: str
    score: int = 0
    label: str = "중립"
    summary: str = ""


@dataclass(frozen=True)
class MarketSentiment:
    """시장 전체 감성."""

    overall_score: int = 0
    overall_label: str = "중립"
    stock_scores: tuple[SentimentScore, ...] = ()
    fear_greed: str = "중립"

    def to_text(self) -> str:
        lines = [
            "【감성 분석】",
            f"  시장 전체: {self.overall_score:+d} [{self.overall_label}] | 공포탐욕: {self.fear_greed}",
        ]
        for s in self.stock_scores:
            lines.append(f"  {s.name}: {s.score:+d} [{s.label}] -- {s.summary}")
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


def _build_sentiment(data: dict) -> MarketSentiment:
    """파싱된 JSON dict를 MarketSentiment로 변환."""
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


def analyze_sentiment(
    news_text: str,
    stock_names: list[str],
) -> MarketSentiment:
    """뉴스 텍스트에서 감성 점수 추출.

    1차: Claude CLI(json_schema), 2차 폴백: Gemini. 둘 다 실패 시 중립.
    """
    stocks_str = ", ".join(stock_names)
    prompt = f"""다음 뉴스를 읽고 감성 점수를 매겨줘.
점수: -100(극도 부정) ~ +100(극도 긍정).
overall은 시장 전체, stocks는 개별 종목별 감성.

분석 대상: {stocks_str}

뉴스:
{news_text[:8000]}"""

    # 1차: Claude CLI (Max 구독 활용, API 비용 $0)
    cli_result = _analyze_sentiment_cli(prompt)
    if cli_result is not None:
        return cli_result

    # 2차 폴백: Gemini CLI (OAuth 무료 모드, 크레딧 무관)
    gem_cli_result = _analyze_sentiment_gemini_cli(prompt)
    if gem_cli_result is not None:
        return gem_cli_result

    # 3차 폴백: Gemini SDK (API 키, 크레딧 필요)
    if not GEMINI_API_KEY:
        return MarketSentiment()
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=4000,
                response_mime_type="application/json",
                response_schema=_SENTIMENT_SCHEMA,
            ),
        )
        raw = response.text.strip() if response.text else ""
        if not raw:
            log.warning("감성 분석: 빈 응답")
            return MarketSentiment()
        return _build_sentiment(json.loads(raw))
    except Exception as e:
        log.warning(f"감성 분석 실패: {e}")
        return MarketSentiment()


def _analyze_sentiment_cli(prompt: str) -> MarketSentiment | None:
    """Claude CLI로 감성 분석. 성공 시 MarketSentiment, 실패 시 None(폴백 유도)."""
    from core.claude_cli import claude_cli

    try:
        raw = claude_cli(
            prompt,
            model="haiku",
            timeout=180,
            json_schema=json.dumps(_SENTIMENT_SCHEMA, ensure_ascii=False),
        )
        if not raw:
            return None
        return _build_sentiment(json.loads(raw))
    except Exception as e:  # noqa: BLE001 - CLI 실패는 폴백으로 흡수
        log.warning(f"감성 분석 CLI 실패: {e}")
        return None


def _analyze_sentiment_gemini_cli(prompt: str) -> MarketSentiment | None:
    """Gemini CLI(OAuth)로 감성 분석. 성공 시 MarketSentiment, 실패 시 None."""
    from core.gemini_cli import gemini_cli

    schema_hint = (
        '\n\n반드시 다음 JSON 형식으로만 출력: '
        '{"overall":{"score":정수,"summary":"문자열"},'
        '"stocks":[{"name":"문자열","score":정수,"summary":"문자열"}]}'
    )
    try:
        raw = gemini_cli(prompt + schema_hint, timeout=120, want_json=True)
        if not raw:
            return None
        return _build_sentiment(json.loads(raw))
    except Exception as e:  # noqa: BLE001
        log.warning(f"감성 분석 Gemini CLI 실패: {e}")
        return None
