"""
AI 분석 엔진 — 멀티 에이전트 + 기술지표 + 감성 분석 통합

Phase 1 아키텍처:
  1) Gemini → 뉴스 수집
  2) 기술 지표 계산 (로컬, pandas)
  3) 감성 점수 산출 (Gemini Flash)
  4) 4개 페르소나 분석 (Haiku × 4, 병렬)
  5) 종합 판단 (Sonnet)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from config.settings import KST, get_market_config

try:
    from config.settings import WATCHLIST
except ImportError:
    WATCHLIST: dict[str, str] = {}

from core.market import fmt_change, fmt_price
from core.models import BriefingResult, MarketSnapshot, Signal
from core.news import gather_news

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 데일리 리뷰(US_CLOSE) 가드 — 신규 거래 지시 차단
# ═══════════════════════════════════════════════════════
# 데일리 리뷰는 결산·복기·현황 진단 전용. 구조화 필드를 비우는 것만으론 부족하고,
# 본문 자연어에 들어가는 "미래 실행 지시"도 차단한다. 단 과거 거래 복기는 허용.

import re as _re

# 항상 금지 — 과거 복기형이 없는 명백한 미래 실행 지시/권고
_DR_HARD_BANNED: tuple[str, ...] = (
    "매수하세요", "매도하세요", "매수 권고", "매도 권고", "매수 추천", "매도 추천",
    "신규 진입", "추가매수", "추가 매수", "비중 확대", "비중 축소", "예약 주문",
    "오늘 실행", "09:00 전 실행", "오늘의 액션", "매수 후보", "매도 후보",
)

# 문맥 의존 — 미래형 동사와 결합할 때만 금지 (익절/손절/청산 그 자체는 복기에서 허용)
_DR_FUTURE_DIRECTIVE = _re.compile(
    r"(익절|손절|청산|매수|매도)\s*\S{0,3}?(하라|하세요|하자|하시|해야|할\s|할까|"
    r"권고|추천|검토하|실행하|들어가|진입하|담으|받으세요)"
)

# 과거 복기 문맥 — 위 매칭이라도 이 패턴이면 허용 (어제/지난/전날 + 과거형)
_DR_PAST_REVIEW = _re.compile(
    r"(어제|지난|전날|간밤|앞서)\s*\S{0,6}?(익절|손절|청산|매수|매도)\s*\S{0,2}?(한|했|하던|함)"
)


def _detect_daily_review_violations(text: str) -> list[str]:
    """데일리 리뷰 본문에서 미래 실행 지시 표현을 탐지. 과거 복기는 제외.

    Returns: 위반 표현 리스트 (없으면 빈 리스트).
    """
    if not text:
        return []
    violations: list[str] = []
    for phrase in _DR_HARD_BANNED:
        if phrase in text:
            violations.append(phrase)
    # 문맥 의존: 미래 지시 매칭 중, 과거 복기 범위에 속하지 않는 것만
    past_spans = [m.span() for m in _DR_PAST_REVIEW.finditer(text)]
    for m in _DR_FUTURE_DIRECTIVE.finditer(text):
        s, e = m.span()
        in_past = any(ps <= s and e <= pe + 6 for ps, pe in past_spans)
        if not in_past:
            violations.append(m.group(0).strip())
    return violations


def _soften_daily_review_text(text: str) -> str:
    """탐지된 미래 실행 지시를 '관찰 포인트' 톤으로 완화. 과거 복기는 보존."""
    if not text:
        return text
    out = text
    # 하드 금지어 → 중립 표현으로 치환
    replacements = {
        "매수하세요": "매수는 KR_OPEN에서 재확인", "매도하세요": "매도는 KR_OPEN에서 재확인",
        "매수 권고": "매수 관찰 포인트", "매도 권고": "매도 관찰 포인트",
        "매수 추천": "매수 관찰 포인트", "매도 추천": "매도 관찰 포인트",
        "신규 진입": "신규 진입 여부는 KR_OPEN 확인", "추가매수": "추가 매수 여부는 KR_OPEN 확인",
        "추가 매수": "추가 매수 여부는 KR_OPEN 확인",
        "비중 확대": "비중 조정은 KR_OPEN 확인", "비중 축소": "비중 조정은 KR_OPEN 확인",
        "예약 주문": "예약 주문은 KR_OPEN/US_NIGHT에서",
        "오늘 실행": "KR_OPEN에서 재확인", "09:00 전 실행": "KR_OPEN에서 재확인",
        "오늘의 액션": "관찰 포인트", "매수 후보": "관찰 종목", "매도 후보": "관찰 종목",
    }
    for bad, good in replacements.items():
        out = out.replace(bad, good)
    return out


_DR_TEXT_FIELDS = (
    "market_summary", "strategy_summary", "advisor_oneliner",
    "advisor_conclusion", "advisor_verdict", "next_action",
)


def _enforce_daily_review(data: dict) -> tuple[dict, list[str]]:
    """US_CLOSE 데일리 리뷰 강제: 구조화 필드 비우기 + 본문 미래지시 완화.

    Returns: (정리된 data, 위반 경고 리스트)
    """
    warnings: list[str] = []

    # 1) 구조화 거래 필드 강제 제거 (이중 안전장치)
    for f in ("actions", "strategy_buy", "strategy_sell", "night_orders"):
        if data.get(f):
            warnings.append(f"데일리 리뷰: {f} 제거됨")
        data[f] = []
    data["investment_decision"] = "관망"

    # 2) 본문 자연어 미래 실행 지시 탐지 + 완화
    for field in _DR_TEXT_FIELDS:
        val = data.get(field)
        if not isinstance(val, str) or not val:
            continue
        viol = _detect_daily_review_violations(val)
        if viol:
            warnings.append(f"데일리 리뷰 본문 실행지시 완화({field}): {', '.join(dict.fromkeys(viol))[:80]}")
            data[field] = _soften_daily_review_text(val)

    # advisor_verdict는 의사결정 단어 자체가 부적절 → 결산 톤으로 고정
    if data.get("advisor_verdict") not in ("", "결산 완료", "복기"):
        data["advisor_verdict"] = "결산 완료"

    return data, warnings


def _promote_to_actions(data: dict, briefing_type: str) -> int:
    """strategy_buy/strategy_sell를 actions로 자동 승격.

    AI가 분석 기록(strategy_buy)에는 매수를 넣으면서 실행 지시(actions)는 비우는
    경우가 잦음 → 텔레그램이 actions 우선이라 "액션 없음"으로 표시되어 매수 추천이
    사용자에게 안 보이던 버그. actions가 이미 있으면 그대로 두고, 비었을 때만 승격.

    Returns: 승격된 액션 수.
    """
    existing = data.get("actions")
    if existing:  # AI가 직접 채웠으면 존중
        return 0

    is_night = briefing_type in ("KR_NIGHT", "US_NIGHT")
    buy_type = "예약매수" if is_night else "매수·즉시"
    sell_type = "예약매도" if is_night else "매도·즉시"

    actions: list[dict] = []
    for row in data.get("strategy_buy", []) or []:
        if not row.get("ticker") and not row.get("name"):
            continue
        actions.append({
            "type": buy_type,
            "account": row.get("account", ""),
            "ticker": row.get("ticker", ""),
            "name": row.get("name", ""),
            "horizon": row.get("horizon", ""),
            "order_method": "지정가",
            "price": row.get("entry_price", ""),
            "qty": row.get("shares", ""),
            "validity": "당일" if not is_night else "예약",
            "target": row.get("target_price", ""),
            "stop": row.get("stop_loss", ""),
            "cancel_if": row.get("invalidation_condition", ""),
            "long_term_plan": row.get("long_term_plan", ""),
            "reason": row.get("reason", ""),
        })
    for row in data.get("strategy_sell", []) or []:
        if not row.get("ticker") and not row.get("name"):
            continue
        actions.append({
            "type": sell_type,
            "account": row.get("account", ""),
            "ticker": row.get("ticker", ""),
            "name": row.get("name", ""),
            "horizon": row.get("horizon", ""),
            "order_method": "지정가",
            "price": row.get("current_price", "") or row.get("take_profit", ""),
            "qty": row.get("shares", ""),
            "validity": "당일" if not is_night else "예약",
            "target": row.get("take_profit", ""),
            "stop": row.get("stop_loss", ""),
            "cancel_if": row.get("invalidation_condition", ""),
            "long_term_plan": "",
            "reason": row.get("reason", ""),
        })

    if actions:
        data["actions"] = actions
    return len(actions)


def _fetch_watchlist_text(snapshot_prices: dict[str, float] | None = None) -> str:
    """Watchlist 종목 가격 + RSI + MA를 텍스트로 변환. 보유 외 신규 후보.

    snapshot_prices가 있으면 시세를 재조회하지 않고 활용 (API 호출 절약).
    """
    if not WATCHLIST:
        return ""

    try:
        import yfinance as yf
    except ImportError:
        return ""

    # 보유 종목은 제외 (Watchlist에 있지만 이미 보유 중인 종목)
    from config.settings import HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION
    held: set[str] = set()
    for h in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
        held.update(h.keys())

    lines = ["【신규 매수 후보 (Watchlist) — 보유 외, RSI/MA 기준 검토】"]

    for tk, name in WATCHLIST.items():
        if tk in held:
            continue  # 이미 보유 중이면 스킵

        try:
            ticker = yf.Ticker(tk)
            hist = ticker.history(period="60d")
            if len(hist) < 14:
                lines.append(f"  {name}({tk}): 데이터 부족")
                continue

            close = hist["Close"]
            # snapshot 가격 우선, 없으면 yfinance
            price = snapshot_prices.get(tk, float(close.iloc[-1])) if snapshot_prices else float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) > 1 else price
            pct = (price - prev) / prev * 100 if prev > 0 else 0.0

            # RSI(14) — indicators.py 공용 로직 재사용
            from core.indicators import compute_rsi
            rsi = compute_rsi(close)

            # MA20 / MA60
            ma20 = float(close.tail(20).mean()) if len(close) >= 20 else price
            ma60 = float(close.tail(60).mean()) if len(close) >= 60 else price

            # 통화
            unit = "₩" if (".KS" in tk or ".KQ" in tk) else "$"

            lines.append(
                f"  {name}({tk}): {unit}{price:,.0f} "
                f"({pct:+.2f}%) | RSI {rsi:.0f} | MA20 {unit}{ma20:,.0f} | MA60 {unit}{ma60:,.0f}",
            )
        except Exception as e:
            lines.append(f"  {name}({tk}): 데이터 조회 실패 ({type(e).__name__})")

    return "\n".join(lines)


def _backtest_targets(
    briefing_type: str,
    portfolio: dict[str, str],
    max_targets: int = 4,
) -> list[str]:
    """백테스트 대상 종목을 실제 보유 종목에서 동적 생성.

    보유 종목 우선, 부족하면 Watchlist에서 보충. briefing_type에 따라 시장 필터링.
    """
    from config.settings import (
        HOLDINGS_GENERAL,
        HOLDINGS_IRP,
        HOLDINGS_ISA,
        HOLDINGS_PENSION,
        HOLDINGS_RIA,
    )

    def _is_kr(tk: str) -> bool:
        return ".KS" in tk or ".KQ" in tk

    if briefing_type in ("KR_BEFORE", "KR_NIGHT", "KR_OPEN"):
        market_ok = _is_kr
    elif briefing_type in ("US_BEFORE", "US_NIGHT"):
        market_ok = lambda tk: not _is_kr(tk)  # noqa: E731
    else:
        market_ok = lambda tk: True  # noqa: E731

    targets: list[str] = []
    # 1순위: 보유 종목 (순서 유지 + 중복 제거)
    for holdings in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
        for tk in holdings:
            if tk not in targets and tk in portfolio and market_ok(tk):
                targets.append(tk)
    # 2순위: Watchlist 신규 후보
    for tk in WATCHLIST:
        if tk not in targets and tk in portfolio and market_ok(tk):
            targets.append(tk)

    return targets[:max_targets]


def _build_full_context(
    snapshot: MarketSnapshot,
    gathered_news: str,
    indicators_text: str = "",
    sentiment_text: str = "",
    risk_text: str = "",
    backtest_text: str = "",
    kr_market_text: str = "",
    fundamentals_text: str = "",
    watchlist_text: str = "",
) -> str:
    """멀티 에이전트에 전달할 통합 시장 컨텍스트 생성."""
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    idx_lines = []
    for nm, q in snapshot.indices.items():
        idx_lines.append(f"  {nm}: {q.price:,.2f} ({q.pct:+.2f}%)")
    idx = "\n".join(idx_lines)

    mac_lines = []
    for nm, q in snapshot.macro.items():
        mac_lines.append(f"  {nm}: {q.price:,.2f} ({q.pct:+.2f}%)")
    mac = "\n".join(mac_lines)

    stk_lines = []
    for tk, q in snapshot.stocks.items():
        news_list = snapshot.news.get(q.name, [])
        ns = " / ".join(news_list[:2]) if news_list else "-"
        stk_lines.append(
            f"  {q.name}({tk}): {fmt_price(tk, q.price)} "
            f"({q.pct:+.2f}% / {fmt_change(tk, q.change)}) "
            f"H:{fmt_price(tk, q.high)} L:{fmt_price(tk, q.low)} | {ns}"
        )
    stk = "\n".join(stk_lines)

    context = f"""현재 시각: {now}

━━━ yfinance 실시간 데이터 ━━━
【시장 지수】
{idx}

【매크로 지표】
{mac}

【포트폴리오 (통화 포함 현재가)】
{stk}"""

    if watchlist_text:
        context += f"\n\n{watchlist_text}"

    context += f"""

━━━ 실시간 뉴스 (Gemini Google Search) ━━━
{gathered_news}"""

    if indicators_text:
        context += f"\n\n━━━ 기술 지표 (RSI/MACD/볼린저/OBV) ━━━\n{indicators_text}"

    if sentiment_text:
        context += f"\n\n━━━ 감성 분석 ━━━\n{sentiment_text}"

    if risk_text:
        context += f"\n\n━━━ 리스크 분석 (ATR/상관관계/낙폭) ━━━\n{risk_text}"

    if backtest_text:
        context += f"\n\n━━━ 백테스트 검증 ━━━\n{backtest_text}"

    if kr_market_text:
        context += f"\n\n━━━ 한국 시장 심층 (기관/외국인/펀더멘털) ━━━\n{kr_market_text}"

    if fundamentals_text:
        context += f"\n\n━━━ 재무 데이터 (PER/EPS/매출/실적일정) ━━━\n{fundamentals_text}"

    return context


def _parse_signals(raw: list[dict], kind: str) -> tuple[Signal, ...]:
    signals = []
    for row in raw:
        signals.append(
            Signal(
                ticker=row.get("ticker", ""),
                name=row.get("name", ""),
                signal=kind,
                reason=row.get("reason", ""),
                entry_price=row.get("entry_price", ""),
                target_price=row.get("target_price", row.get("take_profit", "")),
                stop_loss=row.get("stop_loss", ""),
                urgency=row.get("urgency", ""),
                shares=row.get("shares", ""),
                timing=row.get("timing", ""),
                split_plan=row.get("split_plan", ""),
            )
        )
    return tuple(signals)


def _extract_balanced_object(text: str) -> str:
    """텍스트에서 첫 균형 잡힌 최상위 {...} 객체를 추출.

    문자열 리터럴과 이스케이프를 인식하여 문자열 내부의 중괄호는 무시한다.
    모델이 JSON 앞뒤에 산문(예: "다음은 결과입니다:")을 붙인 경우 대응.
    """
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""  # 닫히지 않음 (truncation 등)


def _parse_json(raw_text: str) -> dict:
    """Claude/Gemini 응답에서 JSON 추출. 문법 오류 자동 복구."""
    data: dict = {}
    candidates: list[str] = []

    if "```" in raw_text:
        for part in raw_text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part:
                candidates.append(part)
    candidates.append(raw_text)

    # 산문이 섞인 응답 대응: 균형 중괄호로 본체 추출한 후보 추가
    balanced = _extract_balanced_object(raw_text)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        for text in [candidate, _fix_json(candidate)]:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, ValueError):
                continue

    log.error(f"JSON 파싱 최종 실패 (길이: {len(raw_text)})")
    raise json.JSONDecodeError("JSON 파싱 실패", raw_text, 0)


def _fix_json(text: str) -> str:
    """흔한 JSON 문법 오류 자동 수정."""
    import re
    # trailing comma 제거: ,} 또는 ,]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    # 작은따옴표 → 큰따옴표
    text = text.replace("'", '"')
    # 제어 문자 제거
    text = re.sub(r'[\x00-\x1f]+', ' ', text)
    return text


def _build_briefing_result(data: dict) -> BriefingResult:
    """JSON 데이터를 BriefingResult로 변환."""
    portfolio_signals = []
    for row in data.get("portfolio_rows", []):
        portfolio_signals.append(
            Signal(
                ticker=row.get("ticker", ""),
                name=row.get("name", ""),
                signal=row.get("signal", "관망"),
                reason=row.get("reason", ""),
            )
        )

    return BriefingResult(
        title=data.get("title", ""),
        market_status=data.get("market_status", "혼조"),
        investment_decision=data.get("investment_decision", "관망"),
        market_summary=data.get("market_summary", ""),
        portfolio_signals=tuple(portfolio_signals),
        buy_signals=_parse_signals(data.get("strategy_buy", []), "매수"),
        sell_signals=_parse_signals(data.get("strategy_sell", []), "매도"),
        advisor_verdict=data.get("advisor_verdict", ""),
        advisor_oneliner=data.get("advisor_oneliner", ""),
        advisor_conclusion=data.get("advisor_conclusion", ""),
        strategy_summary=data.get("strategy_summary", ""),
        raw_json=data,
    )


def analyze(snapshot: MarketSnapshot, briefing_type: str = "MANUAL") -> BriefingResult:
    """멀티 에이전트 파이프라인으로 시장 분석.

    Args:
        snapshot: 시장 데이터 스냅샷
        briefing_type: KR_BEFORE(한국 중심), US_BEFORE(미국 중심), MANUAL(전체)

    파이프라인:
       1) Gemini → 뉴스 수집
       2) 시장 레짐 감지 (VIX/모멘텀, 로컬)
       3) 기술 지표 계산 (로컬)
       4) 감성 점수 산출 (Gemini Flash)
       5) 리스크 분석 (ATR/상관관계/낙폭, 로컬)
       6) 백테스트 검증 (로컬)
       7) 한국 시장 심층 데이터 (KRX)
       8) AI 메모리 조회 + 미결 추천 평가
       9) 멀티모달 차트 분석 (Gemini Vision)
      10) 4개 페르소나 분석 (Haiku × 4, 병렬)
      11) 종합 판단 (Sonnet)

    Raises:
        ValueError: API 키 미설정
        json.JSONDecodeError: Claude 응답 파싱 실패
    """
    portfolio, _, _ = get_market_config(briefing_type)

    from core.backtest import (
        backtest_all_strategies,
        backtest_regime_aware,
        backtest_to_text,
        optimize_rsi_params,
    )
    from core.chart_vision import analyze_key_charts, chart_analyses_to_text
    from core.indicators import calculate_all, calculate_sector_momentum, indicators_to_text
    from core.kr_market import (
        fetch_cumulative_flows,
        fetch_fundamentals,
        fetch_institutional_flow,
        kr_market_to_text,
    )
    from core.memory import (
        evaluate_open_predictions,
        generate_open_positions_review,
        memory_to_text,
        save_predictions_from_briefing,
    )
    from core.multi_agent import run_all_personas, synthesize
    from core.regime import detect_regime
    from core.risk import generate_risk_report, risk_report_to_text
    from core.sentiment import analyze_sentiment

    # CLI 기반 무거운 작업(뉴스 WebSearch, 차트 비전)을 백그라운드로 선제 실행 →
    # 로컬 단계(레짐/지표/리스크/백테스트/재무)와 시간 겹침으로 전체 단축.
    from concurrent.futures import ThreadPoolExecutor
    _bg_executor = ThreadPoolExecutor(max_workers=3)

    # 1단계: 뉴스 수집 (CLI WebSearch) — 백그라운드 시작
    log.info("[1/11] 뉴스 수집 시작 (백그라운드)... (유형: %s)", briefing_type)
    news_future = _bg_executor.submit(gather_news, briefing_type)
    # 9단계 차트 비전(CLI)도 동시에 백그라운드 시작
    charts_future = _bg_executor.submit(analyze_key_charts, portfolio)
    # 시장 기회 스캐너 (워치리스트 밖 급등/주도주 탐지)도 백그라운드
    from core.scanner import scan_to_text
    scanner_future = _bg_executor.submit(scan_to_text, briefing_type)

    # 2단계: 시장 레짐 감지 (로컬) — KR 브리핑은 KOSPI 기준, 그 외 S&P500 기준
    log.info("[2/11] 시장 레짐 감지 중...")
    regime_market = "KR" if briefing_type in ("KR_BEFORE", "KR_NIGHT", "KR_OPEN") else "US"
    regime = detect_regime(regime_market)
    regime_text = regime.to_text()
    if briefing_type in ("MANUAL", "US_CLOSE"):
        # 통합/데일리리뷰: KOSPI 레짐도 병행 표시
        kr_regime = detect_regime("KR")
        if kr_regime.confidence > 0:
            regime_text += f"\n{kr_regime.to_text()}"
    log.info(f"  레짐: {regime.regime} ({regime.confidence}%, {regime.index_name}) — {regime.risk_adjustment}")

    # 3단계: 기술 지표 계산 (로컬)
    log.info("[3/11] 기술 지표 계산 중...")
    indicators = calculate_all(portfolio)
    indicators_text = indicators_to_text(indicators)

    # 3.5단계: 섹터 모멘텀 (로컬)
    log.info("[3.5/13] 섹터 모멘텀 계산 중...")
    try:
        sector_momentum_text = calculate_sector_momentum()
    except Exception as e:
        log.warning("섹터 모멘텀 계산 실패: %s", e)
        sector_momentum_text = ""

    # 4단계: 감성 분석 (뉴스 결과 수합 후) — 뉴스가 아직이면 여기서 대기
    log.info("[4/11] 뉴스 수합 + 감성 분석 중...")
    gathered_news = news_future.result()
    stock_names = list(portfolio.values())
    _news_failed = not gathered_news or gathered_news.startswith("(뉴스 수집 실패")
    if _news_failed:
        log.warning("뉴스 수집 실패 → 감성 분석 스킵")
        sentiment_text = ""
    else:
        sentiment = analyze_sentiment(gathered_news, stock_names)
        sentiment_text = sentiment.to_text()

    # 5단계: 리스크 분석 (변동성+상관관계+서킷브레이커, 로컬)
    log.info("[5/11] 리스크 분석 중 (변동성+상관관계+서킷브레이커)...")
    from config.settings import DEFAULT_CASH
    from core.memory import get_accuracy_summary, get_recent_predictions
    memory_stats = get_accuracy_summary()
    recent_preds = get_recent_predictions(20)
    recent_outcomes = [p.outcome for p in recent_preds if p.outcome]
    risk_report = generate_risk_report(
        portfolio, DEFAULT_CASH,
        memory_stats=memory_stats,
        recent_outcomes=recent_outcomes,
    )
    risk_text = risk_report_to_text(risk_report)
    log.info(f"  전체 리스크: {risk_report.overall_risk}")
    if risk_report.circuit_breaker.is_locked:
        log.warning(f"  🚨 서킷 브레이커: {risk_report.circuit_breaker.reason}")

    # 6단계: 백테스트 (기본 + 레짐별 + 최적화, 로컬)
    log.info("[6/11] 백테스트 검증 중 (레짐별 + 파라미터 최적화)...")
    backtest_results = []
    key_tickers = _backtest_targets(briefing_type, portfolio)
    log.info("  백테스트 대상: %s", ", ".join(key_tickers) if key_tickers else "(없음)")
    for tk in key_tickers:
        if tk in portfolio:
            bt = backtest_all_strategies(tk, portfolio[tk], "1y")
            backtest_results.extend(bt)
            # 레짐별 전략
            regime_bt = backtest_regime_aware(tk, portfolio[tk], "1y", regime.regime)
            if regime_bt:
                backtest_results.append(regime_bt)
            # 최적 파라미터
            opt = optimize_rsi_params(tk, portfolio[tk], "1y")
            if opt:
                backtest_results.append(opt)
    backtest_text = backtest_to_text(backtest_results)

    # 7단계: 재무 데이터 (yfinance)
    log.info("[7/13] 재무 데이터 수집 중 (PER/EPS/매출/실적일정)...")
    from core.fundamentals import fetch_all_fundamentals, fundamentals_to_text
    fund_data = fetch_all_fundamentals(portfolio)
    fundamentals_text = fundamentals_to_text(fund_data)
    # 실적 임박 경고
    upcoming = [d for d in fund_data if 0 <= d.days_to_earnings <= 7]
    if upcoming:
        for d in upcoming:
            log.warning(f"  !! {d.name} 실적 발표 {d.days_to_earnings}일 후 ({d.earnings_date})")

    # 8단계: 한국 시장 심층 (KRX) — 미국장 브리핑에서는 스킵 (US_CLOSE는 데일리 리뷰라 포함)
    kr_text = ""
    if briefing_type not in ("US_BEFORE", "US_NIGHT"):
        log.info("[8/13] 한국 시장 데이터 조회 중...")
        flows = fetch_institutional_flow()
        fundamentals = fetch_fundamentals()
        # 5일 누적 수급 (네이버 금융 종목별 조회)
        try:
            cumulative = fetch_cumulative_flows(days=5)
        except Exception as e:
            log.warning("누적 수급 조회 실패: %s", e)
            cumulative = None
        kr_text = kr_market_to_text(flows, fundamentals, cumulative)
    else:
        log.info("[8/13] 한국 시장 데이터 스킵 (미국장 브리핑)")

    # 8단계: AI 메모리 — 미결 추천 평가 + 과거 기록 조회
    log.info("[8/13] AI 메모리 조회 중...")
    current_prices = {tk: q.price for tk, q in snapshot.stocks.items()}
    closed = evaluate_open_predictions(current_prices)
    if closed > 0:
        log.info(f"  {closed}건 미결 추천 종료 처리")
    mem_text = memory_to_text()

    # 9단계: 멀티모달 차트 분석 (CLI Vision) — 백그라운드 결과 수합
    log.info("[10/13] 차트 패턴 분석 수합 중 (AI Vision)...")
    try:
        chart_analyses = charts_future.result()
    except Exception as e:
        log.warning("차트 분석 백그라운드 실패: %s", e)
        chart_analyses = []
    _bg_executor.shutdown(wait=False)
    chart_text = chart_analyses_to_text(chart_analyses)
    if chart_analyses:
        log.info(f"  {len(chart_analyses)}종목 차트 분석 완료")

    # 10단계: 매매 제약 조건 계산
    log.info("[11/13] 매매 제약 조건 계산 중...")
    from core.portfolio import compute_allowed_actions, constraints_to_text
    constraints = compute_allowed_actions(current_prices)
    constraints_text = constraints_to_text(constraints, portfolio)

    # 11단계: 통합 컨텍스트 → 4개 페르소나 병렬 분석 (Haiku)
    log.info("[12/13] 4개 페르소나 분석 중 (병렬)...")

    # 추가 컨텍스트
    extra_context = ""

    # 미결 포지션 점검 (이전 브리핑 추천 → 현재 상태 → 유지/매도 강제 판단)
    try:
        positions_review = generate_open_positions_review(current_prices)
        if positions_review:
            extra_context += f"\n\n{positions_review}"
    except Exception as e:
        log.warning("미결 포지션 점검 실패: %s", e)

    # 실적 캘린더 경고 (D-7 이내 실적 발표 종목 강조)
    earnings_warnings = []
    for f in fund_data:
        if f.days_to_earnings is not None and 0 <= f.days_to_earnings <= 7:
            earnings_warnings.append(f"⚠️ {f.name} 실적 발표 D-{f.days_to_earnings}")
    if earnings_warnings:
        extra_context += "\n\n━━━ ⚠️ 실적 캘린더 경고 ━━━\n" + "\n".join(earnings_warnings)
        extra_context += "\n→ 실적 D-3 이내 종목은 이벤트 리스크 명시 필수. 신규 매수 시 실적 후 진입 검토."

    # 경제 캘린더 경고 (D-3 이내 매크로 이벤트)
    from config.settings import ECONOMIC_CALENDAR
    from datetime import datetime as _dt
    today = _dt.now(KST).date()
    econ_warnings = []
    for date_str, event_name, importance in ECONOMIC_CALENDAR:
        try:
            event_date = _dt.strptime(date_str, "%Y-%m-%d").date()
            days_until = (event_date - today).days
            if 0 <= days_until <= 3:
                icon = "🔴" if importance == "HIGH" else "🟡"
                econ_warnings.append(f"{icon} {event_name} D-{days_until} ({date_str})")
        except ValueError:
            continue
    if econ_warnings:
        extra_context += "\n\n━━━ 📅 경제 캘린더 경고 (D-3 이내) ━━━\n" + "\n".join(econ_warnings)
        extra_context += "\n→ HIGH 이벤트 전 신규 포지션 진입 주의. 변동성 확대 예상."

    # US_CLOSE (데일리 리뷰): 어제 사용자의 매매 + 종료된 추천 결과 주입
    if briefing_type == "US_CLOSE":
        try:
            from core.trade_log import daily_review_text
            review = daily_review_text()
            if review:
                extra_context += f"\n\n━━━ 📒 어제의 매매 리뷰 (실현손익 포함) ━━━\n{review}"
        except Exception as e:
            log.debug("매매 리뷰 스킵: %s", e)
        try:
            from core.memory import _get_conn as _mem_conn
            from datetime import timedelta as _td
            _cut = (_dt.now(KST) - _td(hours=26)).isoformat()
            closed_rows = _mem_conn().execute(
                """SELECT name, ticker, signal, pnl_pct, outcome FROM predictions
                   WHERE closed_at >= ? AND outcome IN ('win','loss','neutral')
                   ORDER BY closed_at DESC LIMIT 10""",
                (_cut,),
            ).fetchall()
            if closed_rows:
                _cl = ["어제 종료된 AI 추천 결과:"]
                for r in closed_rows:
                    icon = "✅" if r["outcome"] == "win" else "❌" if r["outcome"] == "loss" else "➖"
                    _cl.append(f"  {icon} {r['name']}({r['ticker']}) {r['signal']}: {r['pnl_pct']:+.1f}% [{r['outcome']}]")
                extra_context += "\n\n━━━ 어제 종료된 추천 ━━━\n" + "\n".join(_cl)
        except Exception as e:
            log.debug("종료 추천 조회 스킵: %s", e)

    # 미반영 매매 경고 (텔레그램 '매매' 명령으로 기록된 settings 미반영분)
    try:
        from core.trade_log import pending_trades_text
        pending = pending_trades_text()
        if pending:
            extra_context += f"\n\n━━━ ⚠️ 미반영 매매 기록 ━━━\n{pending}"
    except Exception as e:
        log.debug("매매 기록 확인 스킵: %s", e)

    # KIS 실잔고 검증 (KIS 계좌에 잔고가 있을 때만 동작 — 현재 보유는 삼성증권)
    try:
        from core.kis_balance import compare_with_settings
        balance_check = compare_with_settings()
        if balance_check:
            extra_context += f"\n\n━━━ 계좌 잔고 검증 ━━━\n{balance_check}"
    except Exception as e:
        log.debug("잔고 검증 스킵: %s", e)

    # 시장 기회 스캐너 결과 수합 (백그라운드)
    try:
        scanner_text = scanner_future.result(timeout=120)
    except Exception as e:
        log.warning("시장 스캐너 실패: %s", e)
        scanner_text = ""
    if scanner_text:
        extra_context += (
            f"\n\n━━━ 🔭 시장 기회 스캐너 + 전시장 발굴 ━━━\n{scanner_text}"
            "\n→ 해석 규칙 (사용자는 이 종목들을 모름 — 발굴과 가이드가 당신의 핵심 임무):"
            "\n  1. 시그널/발굴 종목 중 상위 후보를 advisor_opportunities에 반드시 포함하고,"
            " 각 종목이 '무슨 사업을 하는 회사이고 왜 오르는지' 한 줄로 설명하라."
            "\n  2. 매수 매력이 충분한 종목은 strategy_buy에 정식 추천 — 진입가/손절/목표/수량/계좌까지 완결된 전략."
            "\n  3. ★ 이미 급등한 종목(추격 불가)이라도 펀더멘털 양호(저PER 또는 매출성장+)면 그냥 버리지 마라 —"
            " strategy_buy에 '눌림목 예약(신규진입)'으로 등재하라: entry_price=눌림목 지정가(직전 급등의 38~50% 되돌림"
            " 또는 20일선 부근), execution_condition='지정가 도달 시', strategy_type='신규진입', account 필수, current_price=현재가."
            " actions에는 '예약매수'로만 넣어라 (즉시매수 아님)."
            "\n  4. 보유 종목과 같은 섹터의 더 강한 주도주는 교체(스위칭) 검토를 제시."
            "\n  5. 지속 추적 가치가 있는 종목은 next_action에 'WATCHLIST 등재 제안: 티커 — 사유'로 명시."
            "\n  6. 모르는 종목이라는 이유로 기각하지 마라 — 데이터 기반으로 평가하되, 정보 부족 시 리스크로만 표기."
        )

    if sector_momentum_text:
        extra_context += f"\n\n━━━ 섹터 모멘텀 ━━━\n{sector_momentum_text}"
    if regime_text:
        extra_context += f"\n\n━━━ 시장 레짐 ━━━\n{regime_text}"
    if mem_text:
        extra_context += f"\n\n━━━ AI 메모리 (과거 추천 정확도) ━━━\n{mem_text}"
    if chart_text:
        extra_context += f"\n\n━━━ 차트 패턴 (AI Vision) ━━━\n{chart_text}"
    extra_context += f"\n\n━━━ 매매 제약 (반드시 준수) ━━━\n{constraints_text}"

    log.info("[12.5/13] Watchlist 신규 후보 데이터 수집...")
    try:
        watchlist_text = _fetch_watchlist_text(snapshot_prices=current_prices)
    except Exception as e:
        log.warning(f"Watchlist 수집 실패: {e}")
        watchlist_text = ""

    market_context = _build_full_context(
        snapshot, gathered_news,
        indicators_text, sentiment_text,
        risk_text, backtest_text, kr_text,
        fundamentals_text, watchlist_text,
    )
    market_context += extra_context

    persona_results = run_all_personas(market_context)

    for pa in persona_results:
        log.info(f"  {pa.persona}: {pa.verdict} (확신도 {pa.confidence}%)")

    # 12단계: 종합 판단 (Sonnet)
    log.info("[13/13] 종합 판단 생성 중...")
    try:
        raw_text = synthesize(persona_results, market_context, briefing_type, current_prices)
        data = _parse_json(raw_text)
    except (RuntimeError, Exception) as synth_err:
        log.error(f"synthesis 실패 → 페르소나 요약 fallback: {synth_err}")
        # 페르소나 결과로 최소한의 브리핑 구성
        verdicts = [pa.verdict for pa in persona_results]
        most_common = max(set(verdicts), key=verdicts.count) if verdicts else "관망"
        persona_summary_lines = []
        for pa in persona_results:
            persona_summary_lines.append(
                f"[{pa.persona}] {pa.verdict} ({pa.confidence}%) — {pa.reasoning[:100]}"
            )
        data = {
            "title": f"[FALLBACK] 종합 판단 생성 실패 — 페르소나 요약",
            "market_status": "혼조",
            "investment_decision": "관망",
            "market_summary": f"종합 판단(synthesis) 생성에 실패했습니다. 페르소나 {len(persona_results)}명 분석 결과만 제공합니다.",
            "consensus": f"다수 의견: {most_common}",
            "dissent": "",
            "portfolio_rows": [],
            "strategy_buy": [],
            "strategy_sell": [],
            "strategy_summary": "\n".join(persona_summary_lines),
            "advisor_verdict": most_common,
            "advisor_oneliner": f"synthesis 실패 — 페르소나 다수 의견: {most_common}",
            "advisor_conclusion": "\n".join(persona_summary_lines),
            "advisor_checklist": [],
            "advisor_risks": [f"synthesis 실패로 종합 분석 미제공 — 개별 페르소나 의견만 참고"],
            "advisor_opportunities": [],
            "advisor_scenarios": [],
            "next_action": "종합 판단 없음 — 수동 판단 필요",
            "account_strategy": {},
            "persona_summary": {
                pa.persona: f"{pa.verdict} ({pa.confidence}%)"
                for pa in persona_results
            },
            "night_orders": [],
        }

    # 메타데이터 보존
    if "persona_summary" not in data:
        data["persona_summary"] = {
            pa.persona: f"{pa.verdict} ({pa.confidence}%) — {pa.reasoning[:60]}"
            for pa in persona_results
        }
    # 페르소나 풀 디테일 (메일 HTML용)
    data["persona_details"] = [
        {
            "persona": pa.persona,
            "verdict": pa.verdict,
            "confidence": pa.confidence,
            "reasoning": pa.reasoning,
            "key_factors": list(pa.key_factors),
            "risk_warning": pa.risk_warning,
            "stock_views": list(pa.stock_views),
        }
        for pa in persona_results
    ]
    data["risk_level"] = risk_report.overall_risk
    data["regime"] = regime.regime
    data["regime_adjustment"] = regime.risk_adjustment

    # 데이터 실패 집계 + 품질 경고 수집
    _data_failures = 0
    _quality_warnings: list[str] = []

    # 데일리 리뷰(US_CLOSE): 신규 거래 지시 전면 차단 (구조화 + 자연어)
    if briefing_type == "US_CLOSE":
        data, _dr_warnings = _enforce_daily_review(data)
        for w in _dr_warnings:
            log.info("  %s", w)
    else:
        # actions 자동 승격: AI가 strategy_buy/sell만 채우고 actions를 비운 경우,
        # 텔레그램이 "액션 없음"으로 표시해 매수 추천이 사용자에게 안 보이던 버그 방지.
        _promoted = _promote_to_actions(data, briefing_type)
        if _promoted:
            log.info("  actions 자동 승격: strategy_buy/sell → %d건", _promoted)
    if _news_failed:
        _data_failures += 1
        _quality_warnings.append("뉴스 수집 실패 — 시황/감성 정보 제외")
    if not kr_text and briefing_type not in ("US_BEFORE", "US_NIGHT"):
        _data_failures += 1
        _quality_warnings.append("KRX 기관/외국인 수급 제외")
    if not chart_analyses:
        _data_failures += 1
        _quality_warnings.append("Gemini 차트 비전 제외")
    if not _news_failed and not sentiment_text:
        _data_failures += 1
        _quality_warnings.append("Gemini 감성 분석 제외")
    failed_personas = 4 - len(persona_results)
    if failed_personas > 0:
        _data_failures += failed_personas
        _quality_warnings.append(f"페르소나 {failed_personas}명 분석 실패")

    result = _build_briefing_result(data)
    # 품질 경고를 BriefingResult에 주입
    object.__setattr__(result, "quality_warnings", tuple(_quality_warnings))
    object.__setattr__(result, "data_failures", _data_failures)

    # 추천 기록을 메모리에 저장 (품질 게이트 + 가격 괴리 검증)
    saved = save_predictions_from_briefing(data, data_failures=_data_failures, current_prices=current_prices)
    if saved > 0:
        log.info(f"  {saved}건 추천 기록 메모리에 저장 (데이터실패: {_data_failures}건)")
    elif _data_failures > 0:
        log.warning(f"  데이터 실패 {_data_failures}건 → 품질 게이트 강화 적용")

    return result


def _build_market_context(snapshot: MarketSnapshot) -> str:
    """시장 데이터를 텍스트로 변환 (ask_ai / REPL 공용)."""
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    stk_lines = []
    for tk, q in snapshot.stocks.items():
        stk_lines.append(
            f"  {q.name}({tk}): {fmt_price(tk, q.price)} "
            f"({q.pct:+.2f}%) H:{fmt_price(tk, q.high)} L:{fmt_price(tk, q.low)}"
        )

    return f"""현재 시각: {now}

포트폴리오 현황:
{chr(10).join(stk_lines)}

시장 지수:
{chr(10).join(f'  {nm}: {q.price:,.2f} ({q.pct:+.2f}%)' for nm, q in snapshot.indices.items())}

매크로:
{chr(10).join(f'  {nm}: {q.price:,.2f} ({q.pct:+.2f}%)' for nm, q in snapshot.macro.items())}
"""


ASK_SYSTEM_PROMPT = """당신은 '전략 주식 파트너'. 반말로 대화. 리스크 먼저, 수치 기반.
아부 금지. 모르면 솔직히 모른다고 해.
매수/매도 추천 시: 진입가, 목표가, 손절가 포함.
간결하게 답변. 리포트 형식 금지."""


def ask_ai(
    question: str,
    snapshot: MarketSnapshot,
    history: list[dict] | None = None,
) -> str:
    """자연어 질문에 대해 AI가 시장 데이터 기반으로 답변.

    Args:
        question: 사용자 질문
        snapshot: 시장 데이터 스냅샷
        history: 이전 대화 히스토리 [{"role": "user"/"assistant", "content": "..."}]
                 None이면 단발성 질문으로 처리

    예: "한화에어로스페이스 팔때 됐나?"
    """
    context = _build_market_context(snapshot)
    system = f"{ASK_SYSTEM_PROMPT}\n\n{context}"

    messages: list[dict] = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    from core.oauth import get_client
    client = get_client()
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4000,
        system=system,
        messages=messages,
    )
    return response.content[0].text.strip()
