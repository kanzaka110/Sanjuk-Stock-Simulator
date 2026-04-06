"""
AI 분석 엔진 — Claude Sonnet 4.6 기반 매매 전략 생성
Stock_bot/scripts/briefing.py의 build_prompt() + generate() 로직 추출
"""

from __future__ import annotations

import json
from datetime import datetime

import anthropic

from config.settings import CLAUDE_API_KEY, KST, PORTFOLIO
from core.market import fmt_change, fmt_price
from core.models import BriefingResult, MarketSnapshot, Signal
from core.news import gather_news


def _build_prompt(snapshot: MarketSnapshot, gathered_news: str) -> str:
    """Claude에게 전달할 분석 프롬프트 생성."""
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

    return f"""
당신은 나의 '전략 주식 파트너'입니다.
현재 시각: {now}

━━━ yfinance 실시간 데이터 ━━━
【시장 지수】
{idx}

【매크로 지표】
{mac}

【포트폴리오 (통화 포함 현재가)】
{stk}

━━━ 실시간 뉴스 (Gemini Google Search 수집 결과) ━━━
{gathered_news}

━━━ 브리핑 지침 ━━━
① 과장 형용사 금지 — 반드시 % + 수치 사용
② 리스크를 장점보다 먼저 언급
③ 매수/매도 신호는 [매수/매도/홀딩/관망] + 근거 필수
④ 전략에 반드시 진입 타이밍 (시간대, 조건, 분할 계획) 포함
⑤ 솔직한 조언: 아부 금지. 데이터 기반 직언.

━━━ 출력: 순수 JSON (코드블록 없이) ━━━
{{
  "title": "날짜+시간 + 핵심 요약",
  "market_status": "상승|하락|보합|혼조",
  "investment_decision": "매수실행|매도실행|보류|관망",
  "market_summary": "리스크 먼저, 수치 중심, 400자 이상 상세 분석",
  "portfolio_rows": [
    {{
      "ticker": "종목코드", "name": "종목명",
      "price_display": "₩201,000 또는 $178.56",
      "change_pct": "+0.25%",
      "signal": "매수|매도|홀딩|관망",
      "reason": "수치+뉴스 기반 근거"
    }}
  ],
  "strategy_buy": [
    {{
      "ticker": "코드", "name": "종목명",
      "urgency": "🔥강력|⚡적극|✅일반",
      "current_price": "현재가",
      "entry_price": "진입가 범위",
      "target_price": "목표가",
      "stop_loss": "손절가",
      "shares": "추천 매수 수량",
      "split_plan": "분할 매수 계획",
      "timing": "진입 타이밍",
      "risk_note": "리스크 요약",
      "reason": "매수 근거 상세"
    }}
  ],
  "strategy_sell": [
    {{
      "ticker": "코드", "name": "종목명",
      "urgency": "🔴즉시|🟠주의|🟡모니터링",
      "current_price": "현재가",
      "shares": "추천 매도 수량",
      "take_profit": "익절 목표가",
      "stop_loss": "손절가",
      "timing": "매도 타이밍",
      "reason": "매도 근거"
    }}
  ],
  "strategy_summary": "오늘 가장 중요한 매수/매도 판단 요약. 300자 이상.",
  "advisor_verdict": "매수대기|소액분할|적극매수|매도고려",
  "advisor_oneliner": "한 문장 직언 (수치 포함)",
  "advisor_conclusion": "300자 이상 종합 결론. 팩트 기반."
}}
""".strip()


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


def analyze(snapshot: MarketSnapshot) -> BriefingResult:
    """시장 데이터를 분석하여 AI 브리핑 결과 반환.

    Raises:
        ValueError: API 키 미설정
        json.JSONDecodeError: Claude 응답 파싱 실패
    """
    if not CLAUDE_API_KEY:
        raise ValueError("CLAUDE_API_KEY 환경변수가 설정되지 않았습니다.")

    # 1단계: Gemini로 뉴스 수집
    gathered_news = gather_news()

    # 2단계: Claude로 전략 분석
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    prompt = _build_prompt(snapshot, gathered_news)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=10000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text.strip()

    # JSON 파싱
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

    # Signal 객체로 변환
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


def ask_ai(question: str, snapshot: MarketSnapshot) -> str:
    """자연어 질문에 대해 AI가 시장 데이터 기반으로 답변.

    예: "한화에어로스페이스 팔때 됐나?"
    """
    if not CLAUDE_API_KEY:
        raise ValueError("CLAUDE_API_KEY 환경변수가 설정되지 않았습니다.")

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    # 포트폴리오 현황 텍스트
    stk_lines = []
    for tk, q in snapshot.stocks.items():
        stk_lines.append(
            f"  {q.name}({tk}): {fmt_price(tk, q.price)} "
            f"({q.pct:+.2f}%) H:{fmt_price(tk, q.high)} L:{fmt_price(tk, q.low)}"
        )

    context = f"""현재 시각: {now}

포트폴리오 현황:
{chr(10).join(stk_lines)}

시장 지수:
{chr(10).join(f'  {nm}: {q.price:,.2f} ({q.pct:+.2f}%)' for nm, q in snapshot.indices.items())}

매크로:
{chr(10).join(f'  {nm}: {q.price:,.2f} ({q.pct:+.2f}%)' for nm, q in snapshot.macro.items())}
"""

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[
            {
                "role": "user",
                "content": f"""당신은 '전략 주식 파트너'. 반말로 대화. 리스크 먼저, 수치 기반.
아부 금지. 모르면 솔직히 모른다고 해.

{context}

사용자 질문: {question}""",
            }
        ],
    )
    return response.content[0].text.strip()
