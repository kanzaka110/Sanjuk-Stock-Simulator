"""
뉴스 수집 — Gemini 2.5 Pro + Google Search
Stock_bot/scripts/briefing.py의 gather_news_with_gemini() 로직 추출
"""

from __future__ import annotations

from datetime import datetime

from google import genai
from google.genai import types

from config.settings import GEMINI_API_KEY, KST, PORTFOLIO


def _get_gemini_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
    return genai.Client(api_key=GEMINI_API_KEY)


def gather_news(briefing_type: str = "MANUAL") -> str:
    """Gemini 2.5 Pro + Google Search로 최신 뉴스/분석 수집.

    Args:
        briefing_type: KR_BEFORE(한국 중심), US_BEFORE(미국 중심), MANUAL(전체)

    Returns:
        수집된 뉴스 텍스트. 실패 시 오류 메시지.
    """
    from config.settings import get_market_config

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    portfolio, _, _ = get_market_config(briefing_type)
    stock_names = ", ".join(f"{nm}({tk})" for tk, nm in portfolio.items())

    if briefing_type == "KR_BEFORE":
        prompt = f"""현재 시각: {now}

【한국 시장 중심 브리핑】 다음 항목을 Google Search로 검색하여 최신 정보를 수집해주세요:

1. 국내 증시 핵심 (코스피, 코스닥 동향, 외국인/기관 수급, 프로그램 매매)
2. 한국 주요 업종별 동향 (반도체, 방산, 자동차, 2차전지)
3. 포트폴리오 종목별 최신 뉴스: {stock_names}
4. 증권사 리포트 (삼성전자, 한화에어로스페이스 등)
5. 외국인/기관 매매 동향 상세
6. 원달러 환율 및 국내 금리 동향
7. 오늘 한국 경제 캘린더 (실적 발표, 경제 지표)
8. 전일 미국 증시 결과 (한국장 영향 분석)
9. 한국 ETF 시장 동향 (나스닥100, S&P500 추종 ETF 괴리율)

각 항목별로 핵심 내용을 정리해서 텍스트로 반환해주세요. 출처도 포함해주세요."""
    elif briefing_type == "US_BEFORE":
        prompt = f"""현재 시각: {now}

【미국 시장 중심 브리핑】 다음 항목을 Google Search로 검색하여 최신 정보를 수집해주세요:

1. 미국 증시 핵심 (S&P500, 나스닥, 다우 동향, 선물 시장)
2. 미국 주요 업종별 동향 (반도체, AI, 방산, 빅테크)
3. 포트폴리오 종목별 최신 뉴스: {stock_names}
4. 월가 주요 분석 (Bloomberg, Reuters, WSJ, CNBC)
5. Fed 통화 정책, 금리 전망, FOMC 관련
6. VIX, 미국 10년 국채, 유가, 금 동향
7. 오늘 미국 경제 캘린더 (실적 발표, 경제 지표)
8. 반도체/AI 관련 최신 뉴스 (NVDA, MU 중심)
9. 지정학적 이슈 (무역 갈등, 방산 수주)

각 항목별로 핵심 내용을 정리해서 텍스트로 반환해주세요. 출처도 포함해주세요."""
    else:
        prompt = f"""현재 시각: {now}

다음 항목들을 Google Search로 검색하여 최신 정보를 수집해주세요:

1. 국내 증시 (코스피, 코스닥 오늘 동향, 외국인/기관 수급)
2. 미국 증시 (S&P500, 나스닥, 다우 동향)
3. 매크로 (금리, 환율, 유가, VIX, Fed 동향)
4. 포트폴리오 종목별 최신 뉴스: {stock_names}
5. Bloomberg, Reuters, WSJ, CNBC, FT 전문 분석
6. 증권사 리포트, 외국인/기관 매매 동향
7. 오늘 경제 캘린더 (실적 발표, 경제 지표)
8. 반도체/AI 관련 최신 뉴스

각 항목별로 핵심 내용을 정리해서 텍스트로 반환해주세요. 출처도 포함해주세요."""

    try:
        client = _get_gemini_client()
        google_search_tool = types.Tool(google_search=types.GoogleSearch())
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[google_search_tool],
                max_output_tokens=5000,
            ),
        )
        return response.text.strip()
    except Exception as e:
        return f"(뉴스 수집 실패: {e})"
