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
    # 012450.KS 한화에어로: 2026-06-02 일반 + 06-04 ISA 전량 매도 → WATCHLIST(재진입 후보)로 이동
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

# ─── 보유 종목 투자 시계 (장기/중기/단기) ────────────
# 브리핑 AI에 주입 — 종목별 보유 목적과 관리 전략을 명시.
# horizon: 장기(1년+, 적립/코어) / 중기(3~12개월, 모멘텀) / 단기(~3개월, 트레이딩)
# 사용자가 자유롭게 수정. 매수/매도 판단 시 이 시계 기준으로 평가됨.
HOLDING_STRATEGY: dict[str, dict] = {
    # 장기 코어 (시세 등락으로 매도 판단 금지 — 논지 훼손 시에만)
    "005930.KS": {"horizon": "장기", "thesis": "반도체 코어 보유 (평단 ₩60,425 저가 매집). 사이클 과열 시 부분 익절만, 전량 매도 금지"},
    "360750.KS": {"horizon": "장기", "thesis": "글로벌 코어 적립 (S&P500). 조정 시 추가 적립 대상, 매도 비대상"},
    "133690.KS": {"horizon": "장기", "thesis": "성장 코어 적립 (나스닥100). 조정 시 추가 적립 대상, 매도 비대상"},
    "251350.KS": {"horizon": "장기", "thesis": "선진국 분산. 리밸런싱 시에만 조정"},
    "161510.KS": {"horizon": "장기", "thesis": "배당 인컴. 배당 정책 훼손 시에만 교체 검토"},
    # 중기 모멘텀
    "192090.KS": {"horizon": "중기", "thesis": "중국 정책+AI 모멘텀. 모멘텀 소멸/목표 도달 시 교체"},
    "LMT": {"horizon": "중기", "thesis": "방산 수주 사이클 + 배당. 손절선 관리 중"},
    "462870.KS": {"horizon": "장기", "thesis": "신작 파이프라인 장기 보유: 2026 하반기 '스피릿' 공개 → 2027 스피릿 런칭 + 스텔라블레이드2 2차 공개. 단기 등락/SGF 단발 이벤트로 매도 금지. 무효화 조건: 스피릿 공개 무기연기·핵심 개발진 이탈 등 파이프라인 훼손. 눌림목은 추가 매집 기회"},
    # 단기 트레이딩
    "MU": {"horizon": "단기", "thesis": "HBM 사이클 트레이딩 (+100%+ 수익 중, 부분 익절 진행). 트레일링 익절로 수익 보호"},
}

# ─── 신규 매수 후보 (Watchlist) ─────────────────────
# 보유 외 관심 종목. 분석 시 시장 데이터/지표를 함께 수집해서
# 매수 후보로 검토 가능. 사용자가 자유롭게 추가/제거.
WATCHLIST: dict[str, str] = {
    # 한국 (KOSPI/KOSDAQ 시총·테마 상위)
    # "035420.KS": "NAVER",  # 2026-05-26 ISA 매수 → HOLDINGS_ISA로 이동
    "012450.KS": "한화에어로스페이스",  # 2026-06-04 전량 매도, 재진입 후보로 모니터링
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

# ─── 가격 알림 트리거 (모니터 5분 감시) ──────────────
# 워치리스트/재진입 후보의 지정가 도달 시 텔레그램 즉시 알림.
# 형식: 티커 → {name, below/above (원화/달러), reason}
# below: 가격이 이 값 이하로 떨어지면 알림 (눌림목 매수 기회)
# above: 가격이 이 값 이상으로 오르면 알림 (돌파 확인)
# 사용자가 자유롭게 추가/제거. 브리핑의 "재진입 트리거"를 여기 등록하면 실시간 감시됨.
PRICE_ALERTS: dict[str, dict] = {
    "012450.KS": {"name": "한화에어로스페이스", "below": 950_000, "reason": "재진입 검토가 — 지지 확인 후 ISA 1주 분할 (2026-06-11 브리핑)"},
    "^KS11": {"name": "KOSPI", "below": 7_300, "reason": "RIA KODEX 200 1차 진입 검토 레벨"},
}

# ─── 시장 스캐너 유니버스 ───────────────────────────
# 워치리스트 밖 급등/주도 종목 탐지용 (core/scanner.py).
# SNDK +580% 같은 케이스를 놓치지 않기 위해 매 브리핑마다 스캔.
# 섹터별 유동성 상위 — 사용자가 자유롭게 추가/제거.
SCAN_UNIVERSE_US: dict[str, str] = {
    # 메모리/스토리지 (MU 보유 — 동종 섹터 감시 필수)
    "SNDK": "샌디스크", "WDC": "웨스턴디지털", "STX": "씨게이트",
    # 반도체 설계/파운드리
    "NVDA": "엔비디아", "AMD": "AMD", "AVGO": "브로드컴", "TSM": "TSMC",
    "QCOM": "퀄컴", "ARM": "ARM", "MRVL": "마벨", "TXN": "TI", "INTC": "인텔",
    # 반도체 장비
    "AMAT": "어플라이드", "LRCX": "램리서치", "KLAC": "KLA", "ASML": "ASML",
    # AI 인프라/전력
    "SMCI": "슈퍼마이크로", "VRT": "버티브", "DELL": "델", "ANET": "아리스타",
    "CEG": "컨스털레이션", "VST": "비스트라", "GEV": "GE버노바",
    # 빅테크/소프트웨어
    "AAPL": "애플", "MSFT": "마이크로소프트", "GOOGL": "알파벳", "AMZN": "아마존",
    "META": "메타", "TSLA": "테슬라", "NFLX": "넷플릭스", "ORCL": "오라클",
    "CRM": "세일즈포스", "NOW": "서비스나우", "PLTR": "팔란티어", "SNOW": "스노우플레이크",
    "CRWD": "크라우드스트라이크", "PANW": "팔로알토", "ADBE": "어도비", "IBM": "IBM",
    # 방산/항공 (LMT 보유 — 동종 감시)
    "RTX": "RTX", "NOC": "노스롭", "GD": "제너럴다이내믹스", "LHX": "L3해리스",
    "BA": "보잉", "AXON": "액슨", "RKLB": "로켓랩",
    # 금융
    "JPM": "JP모건", "GS": "골드만삭스", "MS": "모건스탠리", "V": "비자",
    "MA": "마스터카드", "COIN": "코인베이스", "HOOD": "로빈후드",
    # 헬스케어/바이오
    "LLY": "일라이릴리", "UNH": "유나이티드헬스", "NVO": "노보노디스크",
    "MRK": "머크", "PFE": "화이자", "ABBV": "애브비",
    # 에너지/소재
    "XOM": "엑손모빌", "CVX": "셰브론", "OXY": "옥시덴탈", "FCX": "프리포트",
    "ALB": "앨버말", "NEM": "뉴몬트",
    # 소비/산업
    "WMT": "월마트", "COST": "코스트코", "HD": "홈디포", "MCD": "맥도날드",
    "NKE": "나이키", "SBUX": "스타벅스", "CAT": "캐터필러", "DE": "디어",
    "UBER": "우버", "ABNB": "에어비앤비", "DIS": "디즈니",
}

SCAN_UNIVERSE_KR: dict[str, str] = {
    # 반도체
    "005930.KS": "삼성전자", "000660.KS": "SK하이닉스", "042700.KS": "한미반도체",
    "403870.KS": "HPSP", "058470.KQ": "리노공업", "240810.KQ": "원익IPS",
    # 방산/조선/기계
    "012450.KS": "한화에어로스페이스", "047810.KS": "한국항공우주", "079550.KS": "LIG넥스원",
    "064350.KS": "현대로템", "329180.KS": "HD현대중공업", "009540.KS": "HD한국조선해양",
    "042660.KS": "한화오션",
    # 2차전지/소재
    "373220.KS": "LG에너지솔루션", "006400.KS": "삼성SDI", "247540.KQ": "에코프로비엠",
    "086520.KQ": "에코프로", "003670.KS": "포스코퓨처엠",
    # 자동차
    "005380.KS": "현대차", "000270.KS": "기아", "012330.KS": "현대모비스",
    # 바이오/헬스케어
    "207940.KS": "삼성바이오로직스", "068270.KS": "셀트리온", "196170.KQ": "알테오젠",
    "328130.KQ": "루닛", "145020.KQ": "휴젤",
    # 인터넷/게임/엔터
    "035420.KS": "NAVER", "035720.KS": "카카오", "036570.KS": "엔씨소프트",
    "259960.KS": "크래프톤", "462870.KS": "시프트업", "263750.KQ": "펄어비스",
    "352820.KS": "하이브", "041510.KQ": "에스엠",
    # 금융/지주
    "105560.KS": "KB금융", "055550.KS": "신한지주", "086790.KS": "하나금융",
    "316140.KS": "우리금융", "024110.KS": "기업은행",
    # 화학/철강/에너지
    "051910.KS": "LG화학", "005490.KS": "POSCO홀딩스", "010950.KS": "S-Oil",
    "096770.KS": "SK이노베이션",
    # 전력/원전 (AI 전력 수혜)
    "015760.KS": "한국전력", "052690.KS": "한전기술", "034020.KS": "두산에너빌리티",
    # 소비/유통/식품
    "097950.KS": "CJ제일제당", "271560.KS": "오리온", "090430.KS": "아모레퍼시픽",
    # 통신
    "017670.KS": "SK텔레콤", "030200.KS": "KT",
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
    "MU": {"shares": 5, "avg_cost_usd": 408.8181, "ria_eligible": 0},  # 8주 → 6/11 3주 매도 @ $940 (익절, +130%)
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

# RIA B안(해외 매수 자제) 절세 손실 계산용 상수
#   절세 손실 = (매수금액 × 가중치 / RIA 매도금액) × 양도차익 × 세율
RIA_SALES_KRW: float = 20_584_797.0  # RIA 총 매도금액 (원화 환산)
RIA_GAIN_KRW: float = 7_210_348.0    # RIA 양도차익 (원화 환산)
CAPITAL_GAINS_TAX_RATE: float = 0.22  # 해외주식 양도세율 (지방세 포함)

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
# DEFAULT_CASH: 13,429,839 + ~4,300,000(6/11 MU 3주 매도 $2,820 × ₩1,526) = ~17,729,839
# USD 매도대금은 원화 환산 추정치 — 실제 환전/정산 후 조정 필요
DEFAULT_CASH: float = 17_729_839.0

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
    # 미국 CPI (BLS 공식 일정 — OMB PFEI CY2026 기준, 2026-06-10 갱신)
    ("2026-07-14", "미국 CPI 발표", "HIGH"),
    ("2026-08-12", "미국 CPI 발표", "HIGH"),
    ("2026-09-11", "미국 CPI 발표", "HIGH"),
    ("2026-10-14", "미국 CPI 발표", "HIGH"),
    ("2026-11-10", "미국 CPI 발표", "HIGH"),
    ("2026-12-10", "미국 CPI 발표", "HIGH"),
    # 미국 고용보고서 (BLS Employment Situation 공식 일정)
    ("2026-07-02", "미국 고용보고서", "HIGH"),
    ("2026-08-07", "미국 고용보고서", "HIGH"),
    ("2026-09-04", "미국 고용보고서", "HIGH"),
    ("2026-10-02", "미국 고용보고서", "HIGH"),
    ("2026-11-06", "미국 고용보고서", "HIGH"),
    ("2026-12-04", "미국 고용보고서", "HIGH"),
    # 미국 GDP 속보치 (BEA 공식 일정)
    ("2026-07-30", "미국 GDP 속보치 (2Q)", "MEDIUM"),
    ("2026-10-29", "미국 GDP 속보치 (3Q)", "MEDIUM"),
    # 한국은행 금통위 (2026년 8회: 1·2·4·5·7·8·10·11월)
    ("2026-07-17", "한국은행 금통위", "HIGH"),
    ("2026-08-28", "한국은행 금통위", "HIGH"),
    ("2026-10-22", "한국은행 금통위", "HIGH"),
    ("2026-11-26", "한국은행 금통위", "HIGH"),
    # 보유 종목 실적
    ("2026-06-24", "MU 마이크론 실적 발표", "HIGH"),
]
