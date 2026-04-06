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

import anthropic

from config.settings import CLAUDE_API_KEY, KST, PORTFOLIO
from core.market import fmt_change, fmt_price
from core.models import BriefingResult, MarketSnapshot, Signal
from core.news import gather_news

log = logging.getLogger(__name__)


def _build_full_context(
    snapshot: MarketSnapshot,
    gathered_news: str,
    indicators_text: str = "",
    sentiment_text: str = "",
    risk_text: str = "",
    backtest_text: str = "",
    kr_market_text: str = "",
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
{stk}

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


def _parse_json(raw_text: str) -> dict:
    """Claude/Gemini 응답에서 JSON 추출."""
    data: dict = {}
    if "```" in raw_text:
        for part in raw_text.split("```"):
            part = part.strip().lstrip("json").strip()
            try:
                data = json.loads(part)
                break
            except json.JSONDecodeError:
                continue
    if not data:
        data = json.loads(raw_text)
    return data


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


def analyze(snapshot: MarketSnapshot) -> BriefingResult:
    """멀티 에이전트 파이프라인으로 시장 분석.

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
    if not CLAUDE_API_KEY:
        raise ValueError("CLAUDE_API_KEY 환경변수가 설정되지 않았습니다.")

    from core.backtest import backtest_all_strategies, backtest_to_text
    from core.chart_vision import analyze_key_charts, chart_analyses_to_text
    from core.indicators import calculate_all, indicators_to_text
    from core.kr_market import (
        fetch_fundamentals,
        fetch_institutional_flow,
        kr_market_to_text,
    )
    from core.memory import (
        evaluate_open_predictions,
        memory_to_text,
        save_predictions_from_briefing,
    )
    from core.multi_agent import run_all_personas, synthesize
    from core.regime import detect_regime
    from core.risk import generate_risk_report, risk_report_to_text
    from core.sentiment import analyze_sentiment

    # 1단계: Gemini로 뉴스 수집
    log.info("[1/11] 뉴스 수집 중...")
    gathered_news = gather_news()

    # 2단계: 시장 레짐 감지 (로컬)
    log.info("[2/11] 시장 레짐 감지 중...")
    regime = detect_regime()
    regime_text = regime.to_text()
    log.info(f"  레짐: {regime.regime} ({regime.confidence}%) — {regime.risk_adjustment}")

    # 3단계: 기술 지표 계산 (로컬)
    log.info("[3/11] 기술 지표 계산 중...")
    indicators = calculate_all(PORTFOLIO)
    indicators_text = indicators_to_text(indicators)

    # 4단계: 감성 분석 (Gemini Flash)
    log.info("[4/11] 감성 분석 중...")
    stock_names = list(PORTFOLIO.values())
    sentiment = analyze_sentiment(gathered_news, stock_names)
    sentiment_text = sentiment.to_text()

    # 5단계: 리스크 분석 (로컬)
    log.info("[5/11] 리스크 분석 중 (ATR/상관관계/낙폭)...")
    from config.settings import DEFAULT_CASH
    risk_report = generate_risk_report(PORTFOLIO, DEFAULT_CASH)
    risk_text = risk_report_to_text(risk_report)
    log.info(f"  전체 리스크: {risk_report.overall_risk}")

    # 6단계: 백테스트 (로컬, 주요 종목만)
    log.info("[6/11] 백테스트 검증 중...")
    backtest_results = []
    key_tickers = ["NVDA", "005930.KS", "012450.KS", "MU"]
    for tk in key_tickers:
        if tk in PORTFOLIO:
            bt = backtest_all_strategies(tk, PORTFOLIO[tk], "1y")
            backtest_results.extend(bt)
    backtest_text = backtest_to_text(backtest_results)

    # 7단계: 한국 시장 심층 (KRX)
    log.info("[7/11] 한국 시장 데이터 조회 중...")
    flows = fetch_institutional_flow()
    fundamentals = fetch_fundamentals()
    kr_text = kr_market_to_text(flows, fundamentals)

    # 8단계: AI 메모리 — 미결 추천 평가 + 과거 기록 조회
    log.info("[8/11] AI 메모리 조회 중...")
    current_prices = {tk: q.price for tk, q in snapshot.stocks.items()}
    closed = evaluate_open_predictions(current_prices)
    if closed > 0:
        log.info(f"  {closed}건 미결 추천 종료 처리")
    mem_text = memory_to_text()

    # 9단계: 멀티모달 차트 분석 (Gemini Vision)
    log.info("[9/11] 차트 패턴 분석 중 (AI Vision)...")
    chart_analyses = analyze_key_charts(PORTFOLIO, max_charts=4)
    chart_text = chart_analyses_to_text(chart_analyses)
    if chart_analyses:
        log.info(f"  {len(chart_analyses)}종목 차트 분석 완료")

    # 10단계: 통합 컨텍스트 → 4개 페르소나 병렬 분석 (Haiku)
    log.info("[10/11] 4개 페르소나 분석 중 (병렬)...")

    # 레짐 + 메모리 + 차트를 추가 컨텍스트로 삽입
    extra_context = ""
    if regime_text:
        extra_context += f"\n\n━━━ 시장 레짐 ━━━\n{regime_text}"
    if mem_text:
        extra_context += f"\n\n━━━ AI 메모리 (과거 추천 정확도) ━━━\n{mem_text}"
    if chart_text:
        extra_context += f"\n\n━━━ 차트 패턴 (AI Vision) ━━━\n{chart_text}"

    market_context = _build_full_context(
        snapshot, gathered_news,
        indicators_text, sentiment_text,
        risk_text, backtest_text, kr_text,
    )
    market_context += extra_context

    persona_results = run_all_personas(market_context)

    for pa in persona_results:
        log.info(f"  {pa.persona}: {pa.verdict} (확신도 {pa.confidence}%)")

    # 11단계: 종합 판단 (Sonnet)
    log.info("[11/11] 종합 판단 생성 중...")
    raw_text = synthesize(persona_results, market_context)

    data = _parse_json(raw_text)

    # 메타데이터 보존
    if "persona_summary" not in data:
        data["persona_summary"] = {
            pa.persona: f"{pa.verdict} ({pa.confidence}%) — {pa.reasoning[:60]}"
            for pa in persona_results
        }
    data["risk_level"] = risk_report.overall_risk
    data["regime"] = regime.regime
    data["regime_adjustment"] = regime.risk_adjustment

    result = _build_briefing_result(data)

    # 추천 기록을 메모리에 저장
    saved = save_predictions_from_briefing(data)
    if saved > 0:
        log.info(f"  {saved}건 추천 기록 메모리에 저장")

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
    if not CLAUDE_API_KEY:
        raise ValueError("CLAUDE_API_KEY 환경변수가 설정되지 않았습니다.")

    context = _build_market_context(snapshot)
    system = f"{ASK_SYSTEM_PROMPT}\n\n{context}"

    messages: list[dict] = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=system,
        messages=messages,
    )
    return response.content[0].text.strip()
