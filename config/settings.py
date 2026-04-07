"""
설정 모듈 — 환경변수, 포트폴리오, 상수 관리
"""

import os
from datetime import timezone, timedelta
from pathlib import Path

# ─── 타임존 ─────────────────────────────────────────
KST = timezone(timedelta(hours=9))

# ─── 프로젝트 경로 ──────────────────────────────────
ROOT_DIR = Path(__file__).parent.parent
DB_DIR = ROOT_DIR / "db" / "data"

# ─── API 키 (환경변수) ──────────────────────────────
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
CLAUDE_API_KEY: str = os.environ.get(
    "CLAUDE_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "")
)

# ─── Notion API ─────────────────────────────────────
NOTION_API_KEY: str = os.environ.get("NOTION_API_KEY", "")
NOTION_DB_ID: str = os.environ.get("NOTION_DB_ID", "")
NOTION_TOKEN: str = os.environ.get("NOTION_TOKEN", NOTION_API_KEY)
NOTION_DATABASE_ID: str = os.environ.get("NOTION_DATABASE_ID", NOTION_DB_ID)

# ─── 텔레그램 ──────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── 포트폴리오 ─────────────────────────────────────
PORTFOLIO: dict[str, str] = {
    "005930.KS": "삼성전자",
    "012450.KS": "한화에어로스페이스",
    "133690.KS": "TIGER 미국나스닥100",
    "360750.KS": "TIGER 미국S&P500",
    "251350.KS": "KODEX MSCI선진국",
    "161510.KS": "PLUS 고배당주",
    "329200.KS": "TIGER 리츠부동산인프라",
    "192090.KS": "TIGER 차이나CSI300",
    "NVDA": "엔비디아",
    "GOOGL": "구글(알파벳A)",
    "MU": "마이크론",
    "LMT": "록히드마틴",
}

# KRW 통화 판별 (국내 종목)
KRW_TICKERS: set[str] = {t for t in PORTFOLIO if ".KS" in t}

# 시장 지수
INDICES: dict[str, str] = {
    "^KS11": "KOSPI",
    "^KQ11": "KOSDAQ",
    "^GSPC": "S&P500",
    "^IXIC": "NASDAQ",
    "^DJI": "DOW",
}

# 매크로 지표
MACRO: dict[str, str] = {
    "BZ=F": "브렌트유",
    "CL=F": "WTI",
    "USDKRW=X": "원달러(₩)",
    "^VIX": "VIX",
    "^TNX": "미10년국채",
    "GC=F": "금",
}

# ─── API 서버 ──────────────────────────────────────
API_SECRET_KEY: str = os.environ.get("API_SECRET_KEY", "")
API_PORT: int = int(os.environ.get("API_PORT", "8000"))

# ─── 시장별 포트폴리오 분리 ────────────────────────────
KR_PORTFOLIO: dict[str, str] = {
    tk: nm for tk, nm in PORTFOLIO.items() if ".KS" in tk
}
US_PORTFOLIO: dict[str, str] = {
    tk: nm for tk, nm in PORTFOLIO.items() if ".KS" not in tk
}

KR_INDICES: dict[str, str] = {
    "^KS11": "KOSPI",
    "^KQ11": "KOSDAQ",
}
US_INDICES: dict[str, str] = {
    "^GSPC": "S&P500",
    "^IXIC": "NASDAQ",
    "^DJI": "DOW",
}


def get_market_config(briefing_type: str) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """briefing_type에 따라 (portfolio, indices, macro) 반환.

    KR_BEFORE: 한국 종목 + 한국 지수 중심 (미국 지수는 참고용 포함)
    US_BEFORE: 미국 종목 + 미국 지수 중심 (한국 지수는 참고용 포함)
    기타(MANUAL): 전체 포트폴리오
    """
    if briefing_type == "KR_BEFORE":
        return KR_PORTFOLIO, {**KR_INDICES, **US_INDICES}, MACRO
    if briefing_type == "US_BEFORE":
        return US_PORTFOLIO, {**US_INDICES, **KR_INDICES}, MACRO
    return PORTFOLIO, INDICES, MACRO


# ─── 실제 보유 수량 (삼성증권 실데이터 기준, 2026-04-07) ─────
# [일반] 종합계좌
HOLDINGS_GENERAL: dict[str, dict] = {
    "005930.KS": {"shares": 90, "avg_cost_krw": 60_425},
    "NVDA": {"shares": 46, "avg_cost_usd": 132.9104, "ria_eligible": 46},
    "360750.KS": {"shares": 343, "avg_cost_krw": 24_800},
    "MU": {"shares": 11, "avg_cost_usd": 408.8181, "ria_eligible": 0},
    "GOOGL": {"shares": 9, "avg_cost_usd": 318.03, "ria_eligible": 9},
    "012450.KS": {"shares": 2, "avg_cost_krw": 1_314_500},
    "LMT": {"shares": 1, "avg_cost_usd": 639.0, "ria_eligible": 0},
}

# [IRP] 퇴직연금
HOLDINGS_IRP: dict[str, dict] = {
    "133690.KS": {"shares": 30, "avg_cost_krw": 111_077},   # TIGER 미국나스닥100
    "360750.KS": {"shares": 118, "avg_cost_krw": 16_838},   # TIGER 미국S&P500
    "329200.KS": {"shares": 70, "avg_cost_krw": 4_600},     # TIGER 리츠부동산인프라
    "192090.KS": {"shares": 25, "avg_cost_krw": 13_130},    # TIGER 차이나CSI300
}
IRP_CASH: float = 2_780.0
IRP_DEFAULT_OPTION: float = 4_784_915.0  # 디폴트옵션 안정투자형

# [연금저축] CMA
HOLDINGS_PENSION: dict[str, dict] = {
    "133690.KS": {"shares": 69, "avg_cost_krw": 102_974},   # TIGER 미국나스닥100
    "360750.KS": {"shares": 310, "avg_cost_krw": 18_214},   # TIGER 미국S&P500
    "251350.KS": {"shares": 20, "avg_cost_krw": 37_145},    # KODEX MSCI선진국
    "161510.KS": {"shares": 20, "avg_cost_krw": 26_180},    # PLUS 고배당주
}
PENSION_MMF: float = 6_880_513.0  # MMF 잔고

# [ISA] 중개형 ISA (2026-04-07 개설)
HOLDINGS_ISA: dict[str, dict] = {}
ISA_CASH: float = 20_000_000.0

# ─── 예수금 ────────────────────────────────────────
DEFAULT_CASH: float = 3_539_839.0
