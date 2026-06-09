"""
설정 모듈 — 환경변수, 포트폴리오, 상수 관리
"""

import os
from datetime import timezone, timedelta
from pathlib import Path

# ─── 프로젝트 경로 ──────────────────────────────────
ROOT_DIR = Path(__file__).parent.parent
DB_DIR = ROOT_DIR / "db" / "data"

# ─── .env 자동 로드 (쉘 export와 무관하게 안전) ─────
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

# ─── 타임존 ─────────────────────────────────────────
KST = timezone(timedelta(hours=9))

# ─── API 키 (환경변수) ──────────────────────────────
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
# ─── 한국투자증권 KIS API ──────────────────────────
KIS_APP_KEY: str = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET: str = os.environ.get("KIS_APP_SECRET", "")
KIS_HTS_ID: str = os.environ.get("KIS_HTS_ID", "")
KIS_ACCOUNT_NO: str = os.environ.get("KIS_ACCOUNT_NO", "")  # 8자리-2자리
KIS_BASE_URL: str = "https://openapi.koreainvestment.com:9443"

# ─── Notion API ─────────────────────────────────────
NOTION_API_KEY: str = os.environ.get("NOTION_API_KEY", "")
NOTION_DB_ID: str = os.environ.get("NOTION_DB_ID", "")
NOTION_TOKEN: str = os.environ.get("NOTION_TOKEN", NOTION_API_KEY)
NOTION_DATABASE_ID: str = os.environ.get("NOTION_DATABASE_ID", NOTION_DB_ID)

# ─── 텔레그램 ──────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── Gmail SMTP (브리핑/분석 메일 전송) ────────────
GMAIL_USER: str = os.environ.get("GMAIL_USER", "")  # 발송 계정 (예: kanzaka110@gmail.com)
GMAIL_APP_PASSWORD: str = os.environ.get("GMAIL_APP_PASSWORD", "")  # 16자 앱 비밀번호
GMAIL_TO: str = os.environ.get("GMAIL_TO", "")  # 수신자 (미설정 시 GMAIL_USER로)

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
    "462870.KS": "시프트업",
    "035720.KS": "카카오",
    "207940.KS": "삼성바이오로직스",
    "MU": "마이크론",
    "LMT": "록히드마틴",
}

# ─── 신규 매수 후보 (Watchlist) ─────────────────────
# 보유 외 관심 종목. 분석 시 시장 데이터/지표를 함께 수집해서
# 매수 후보로 검토 가능. 사용자가 자유롭게 추가/제거.
WATCHLIST: dict[str, str] = {
    # 한국 (KOSPI/KOSDAQ 시총·테마 상위)
    # "035420.KS": "NAVER",  # 2026-05-26 ISA 매수 → HOLDINGS_ISA로 이동
    "000660.KS": "SK하이닉스",
    "247540.KQ": "에코프로비엠",
    "086520.KQ": "에코프로",
    # 미국
    "AAPL": "애플",
    "MSFT": "마이크로소프트",
    "TSLA": "테슬라",
    "AMD": "AMD",
    "PLTR": "팔란티어",
    "GOOGL": "구글(알파벳A)",  # 2026-05-12 RIA 전량 매도, 재진입 후보로 모니터링
    "NVDA": "엔비디아",  # 2026-05-18 RIA 전량 매도 완료, 재진입 후보로 모니터링
}

# ─── RIA 허용 종목 (국내자산 편입 ETF) ──────────────
# RIA 계좌에서 매수 가능한 국내 ETF — 시세 수집 + 프롬프트 주입 대상
RIA_ALLOWED_TICKERS: dict[str, str] = {
    "069500.KS": "KODEX 200",
    "229200.KS": "KODEX 코스닥150",
    "102110.KS": "TIGER 200",
    "091160.KS": "KODEX 반도체",
    "091180.KS": "KODEX 자동차",
    "122630.KS": "KODEX 레버리지",
    # PLUS 고배당주(161510)는 PORTFOLIO에 이미 포함
}

# KRW 통화 판별 (국내 종목, 보유+watchlist+RIA허용)
KRW_TICKERS: set[str] = (
    {t for t in PORTFOLIO if ".KS" in t}
    | {t for t in WATCHLIST if ".KS" in t or ".KQ" in t}
    | set(RIA_ALLOWED_TICKERS.keys())
)

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


# ─── 시장별 워치리스트 분리 ──────────────────────────
KR_WATCHLIST: dict[str, str] = {
    tk: nm for tk, nm in WATCHLIST.items() if ".KS" in tk or ".KQ" in tk
}
US_WATCHLIST: dict[str, str] = {
    tk: nm for tk, nm in WATCHLIST.items() if ".KS" not in tk and ".KQ" not in tk
}


def get_market_config(briefing_type: str) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """briefing_type에 따라 (portfolio, indices, macro) 반환.

    포트폴리오 + 워치리스트 + RIA 허용 종목을 시장별로 분리.
    워치리스트도 시세 수집 대상에 포함 (신규 매수 후보 추천용).
    """
    if briefing_type in ("KR_BEFORE", "KR_NIGHT"):
        return {**KR_PORTFOLIO, **KR_WATCHLIST, **RIA_ALLOWED_TICKERS}, {**KR_INDICES, **US_INDICES}, MACRO
    if briefing_type in ("US_BEFORE", "US_NIGHT", "US_CLOSE"):
        return {**US_PORTFOLIO, **US_WATCHLIST}, {**US_INDICES, **KR_INDICES}, MACRO
    return {**PORTFOLIO, **WATCHLIST, **RIA_ALLOWED_TICKERS}, INDICES, MACRO


# ─── 실제 보유 수량 (삼성증권 실데이터 기준, 2026-04-07) ─────
# [일반] 종합계좌 (7127450885-01)
# 2026-05-12: NVDA 46주·GOOGL 9주를 RIA 계좌(7179429562-01)로 대체출고
HOLDINGS_GENERAL: dict[str, dict] = {
    "005930.KS": {"shares": 90, "avg_cost_krw": 60_425},
    "360750.KS": {"shares": 243, "avg_cost_krw": 24_800},  # 343주 → 6/2 100주 매도 @ ₩28,500
    "MU": {"shares": 8, "avg_cost_usd": 408.8181, "ria_eligible": 0},  # 11주 → 6/3 3주 매도 @ $1,080 (익절)
    # 012450.KS: 한화에어로 2주 → 2026-06-02 전량 매도 @ ₩1,085,000 (손절, -17.5%)
    "LMT": {"shares": 1, "avg_cost_usd": 639.0, "ria_eligible": 0},
}

# [RIA] 종합(RIA)(비대면) (7179429562-01) — 2026-05-12 일반 계좌에서 대체출고
# 5/31 면제 활용 완료 (2026-05-18 전량 매도)
# 매도 이력:
#   - 2026-05-12: GOOGL 9주 @ $387.00 (전량), NVDA 23주 @ $219.00
#   - 2026-05-14: NVDA 12주 @ $232.00
#   - 2026-05-18: NVDA 11주 @ $228.30 (잔여 0주 — 면제 활용 종료)
# 매수/매도 이력:
#   - 2026-06-02: TIGER 리츠부동산인프라 100주 @ ₩3,900 매수
#   - 2026-06-05: TIGER 리츠부동산인프라 100주 @ ₩3,850 매도 (손절, -1.3%)
HOLDINGS_RIA: dict[str, dict] = {}

# 매도대금 누적 (수수료 차감 후 추정):
#   1차 ≈ ₩12,610,537
#   2차 ≈ ₩4,136,287
#   3차 ≈ ₩3,738,952 ($2,504.97 × ₩1,492.68, 수수료 추정 차감 후)
#   합계 ≈ ₩20,485,776
# RIA_CASH: 20,095,776 + 385,000(6/5 TIGER 리츠 100주 매도) = 20,480,776
RIA_CASH: float = 20_480_776.0

# RIA 5/31 면제 누적 양도차익 (USD) — 최종 확정
#   1차 5/12: GOOGL $620.73 + NVDA $1,980.06 = $2,600.79
#   2차 5/14: NVDA $1,189.08
#   3차 5/18: NVDA $1,049.29
#   합계: $4,839.16 (≈ ₩7,222,932)
RIA_REALIZED_GAIN_USD: float = 4_839.16

# [IRP] 퇴직연금
HOLDINGS_IRP: dict[str, dict] = {
    "133690.KS": {"shares": 30, "avg_cost_krw": 111_077},   # TIGER 미국나스닥100
    "360750.KS": {"shares": 118, "avg_cost_krw": 16_838},   # TIGER 미국S&P500
    # 329200.KS: TIGER 리츠 70주 → 2026-06-05 전량 매도 @ ₩3,860 (-16.1%)
    "192090.KS": {"shares": 25, "avg_cost_krw": 13_130},    # TIGER 차이나CSI300
}
# IRP_CASH: 2,780 + 270,200(6/5 TIGER 리츠 70주 매도) = 272,980
IRP_CASH: float = 272_980.0
IRP_DEFAULT_OPTION: float = 4_784_915.0  # 디폴트옵션 안정투자형

# [연금저축] CMA
HOLDINGS_PENSION: dict[str, dict] = {
    "133690.KS": {"shares": 69, "avg_cost_krw": 102_974},   # TIGER 미국나스닥100
    "360750.KS": {"shares": 310, "avg_cost_krw": 18_214},   # TIGER 미국S&P500
    "251350.KS": {"shares": 20, "avg_cost_krw": 37_145},    # KODEX MSCI선진국
    "161510.KS": {"shares": 20, "avg_cost_krw": 26_180},    # PLUS 고배당주
}
PENSION_MMF: float = 6_880_513.0  # MMF 잔고

# [ISA] 중개형 ISA (7180216799-14, 2026-04-07 개설)
# 매수 이력:
#   - 2026-04-07: TIGER 미국S&P500 200주, TIGER 미국나스닥100 30주 (개설 시)
#   - 2026-05-11: 시프트업 30주 @ ₩31,700, 한화에어로 1주 @ ₩1,320,000
#   - 2026-05-12: 시프트업 30주 @ ₩30,700, PLUS 고배당 13주, 카카오 10주, 삼성바이오 1주, KODEX MSCI선진국 11주
#   - 2026-05-18: 시프트업 30주 @ ₩29,500 (추매)
#   - 2026-05-26: 한화에어로 1주 @ ₩1,250,000 + NAVER 3주 @ ₩200,000 (야간 프리브리핑 지정가 체결)
#   - 2026-05-27: 시프트업 30주 @ ₩28,500 (SGF 카탈리스트 배팅)
HOLDINGS_ISA: dict[str, dict] = {
    "360750.KS": {"shares": 200, "avg_cost_krw": 24_900},     # TIGER 미국S&P500 (4/7)
    "133690.KS": {"shares": 30, "avg_cost_krw": 163_000},     # TIGER 미국나스닥100 (4/7)
    "462870.KS": {"shares": 160, "avg_cost_krw": 30_025},     # 시프트업 (120주@30,100 + 6/5 40주@29,800)
    # 012450.KS: 한화에어로 2주 → 2026-06-04 전량 매도 @ ₩1,080,000 (손절, -16.0%)
    "161510.KS": {"shares": 13, "avg_cost_krw": 28_100},      # PLUS 고배당주 (5/12)
    "251350.KS": {"shares": 11, "avg_cost_krw": 39_940},      # KODEX MSCI선진국 (5/12)
    # 035420.KS: NAVER 3주 → 2026-06-02 전량 매도 @ ₩278,000 (익절, +39.0%)
}
# ISA_CASH: 5,414,360 - 1,192,000(6/5 시프트업 40주@29,800) = 4,222,360
ISA_CASH: float = 4_222_360.0

# ─── 예수금 ────────────────────────────────────────
# DEFAULT_CASH: 8,559,839 + ~4,870,000(6/3 MU 3주 매도 $3,240 × ₩1,503) = ~13,429,839
# USD 매도대금은 원화 환산 추정치 — 실제 환전/정산 후 조정 필요
DEFAULT_CASH: float = 13_429_839.0

# ─── 모니터링 설정 ─────────────────────────────────
MONITOR_INTERVAL_SEC: int = int(os.environ.get("MONITOR_INTERVAL_SEC", "300"))
ALERT_COOLDOWN_SEC: int = int(os.environ.get("ALERT_COOLDOWN_SEC", "3600"))
VIX_THRESHOLD: float = float(os.environ.get("VIX_THRESHOLD", "35.0"))
RSI_LOW_THRESHOLD: float = float(os.environ.get("RSI_LOW_THRESHOLD", "25.0"))
RSI_HIGH_THRESHOLD: float = float(os.environ.get("RSI_HIGH_THRESHOLD", "999.0"))  # 과매수 알림 비활성화
PRICE_CHANGE_THRESHOLD: float = float(os.environ.get("PRICE_CHANGE_THRESHOLD", "7.0"))
CIRCUIT_BREAKER_DRAWDOWN: float = float(os.environ.get("CIRCUIT_BREAKER_DRAWDOWN", "-7.5"))
FX_CHANGE_THRESHOLD: float = float(os.environ.get("FX_CHANGE_THRESHOLD", "0.8"))  # 0.8% 변동
ALLOW_KR_AFTER_HOURS_ALERT: bool = os.environ.get("ALLOW_KR_AFTER_HOURS_ALERT", "false").lower() == "true"

# ─── 경제 캘린더 (매크로 이벤트 수동 등록) ────────────────
# 형식: (날짜, 이벤트명, 중요도)
# 중요도: HIGH(FOMC/CPI/고용), MEDIUM(PPI/ISM), LOW(기타)
ECONOMIC_CALENDAR: list[tuple[str, str, str]] = [
    # 2026 FOMC 일정 (연초 확정)
    ("2026-06-18", "FOMC 금리 결정", "HIGH"),
    ("2026-07-29", "FOMC 금리 결정", "HIGH"),
    ("2026-09-16", "FOMC 금리 결정", "HIGH"),
    ("2026-11-04", "FOMC 금리 결정", "HIGH"),
    ("2026-12-16", "FOMC 금리 결정", "HIGH"),
    # CPI (매월 둘째 주 화~수)
    ("2026-06-11", "미국 CPI 발표", "HIGH"),
    ("2026-07-15", "미국 CPI 발표", "HIGH"),
    # 고용 (매월 첫째 금)
    ("2026-07-02", "미국 고용보고서", "HIGH"),
    # 한국
    ("2026-07-17", "한국은행 금통위", "HIGH"),
    ("2026-08-28", "한국은행 금통위", "HIGH"),
    # 보유 종목 실적
    ("2026-06-24", "MU 마이크론 실적 발표", "HIGH"),
]
