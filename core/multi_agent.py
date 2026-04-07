"""
멀티 에이전트 분석 — 4개 투자자 페르소나 + 종합 판단

ai-hedge-fund 패턴 참고: 각 페르소나가 독립 분석 후
종합 에이전트가 최종 매매 판단을 내린다.

페르소나 에이전트: Haiku 4.5 (비용 절감)
종합 에이전트: Sonnet 4.6 (정확도)
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

import anthropic

from config.settings import CLAUDE_API_KEY, KST

log = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"


# ═══════════════════════════════════════════════════════
# 페르소나 정의
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class PersonaAnalysis:
    """개별 페르소나 분석 결과."""

    persona: str
    verdict: str  # 매수/매도/홀딩/관망
    confidence: int  # 0-100
    reasoning: str
    key_factors: tuple[str, ...] = ()
    risk_warning: str = ""


PERSONAS: dict[str, str] = {
    "가치투자자": """당신은 워렌 버핏 스타일의 가치투자자입니다.
- PER, PBR, ROE, 배당수익률 중심으로 판단
- 내재가치 대비 할인율을 중시
- "좋은 기업을 적정 가격에" 원칙
- 단기 변동보다 장기 펀더멘털 중시
- 과도한 밸류에이션에 대해 경고
- 안전마진(Margin of Safety)을 항상 고려""",

    "성장투자자": """당신은 캐시 우드 스타일의 성장투자자입니다.
- 매출 성장률, TAM(시장 규모), 혁신성 중시
- AI, 반도체, 바이오 등 미래 산업 선호
- 단기 밸류에이션보다 5년 후 성장 잠재력 중시
- 파괴적 혁신(disruptive innovation) 기업 선호
- 높은 변동성을 감수하되, 확신이 있으면 집중""",

    "기술적분석가": """당신은 순수 차트 기반 기술적 분석가입니다.
- RSI, MACD, 볼린저밴드, OBV 등 기술 지표 중심
- 지지선/저항선, 추세선 분석
- 거래량 확인(Volume Confirmation) 필수
- 패턴 인식: 더블탑, 헤드앤숄더, 컵앤핸들 등
- 진입 타이밍과 손절가를 구체적으로 제시
- 추세 추종: 추세에 역행하지 않음""",

    "매크로분석가": """당신은 레이 달리오 스타일의 매크로 분석가입니다.
- 금리, 환율, 유가, VIX 등 거시 경제 중심
- 글로벌 자금 흐름과 통화 정책 분석
- 경기 사이클 위치 판단 (확장/정점/수축/저점)
- 섹터 로테이션 전략
- 지정학적 리스크 (전쟁, 무역갈등) 반영
- 상관관계와 분산 투자 관점""",
}


# ═══════════════════════════════════════════════════════
# 개별 페르소나 분석
# ═══════════════════════════════════════════════════════
# Claude tool_use로 구조화된 출력 강제
_ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "투자 분석 결과를 구조화된 형태로 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["매수", "매도", "홀딩", "관망"],
                "description": "투자 판단",
            },
            "confidence": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "확신도 (0-100)",
            },
            "reasoning": {
                "type": "string",
                "description": "핵심 판단 근거 (200자 이내)",
            },
            "key_factors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "핵심 요인 3개",
            },
            "risk_warning": {
                "type": "string",
                "description": "주요 리스크 (100자 이내)",
            },
        },
        "required": ["verdict", "confidence", "reasoning", "key_factors", "risk_warning"],
    },
}


def _run_persona(
    persona_name: str,
    persona_prompt: str,
    market_context: str,
) -> PersonaAnalysis:
    """단일 페르소나 분석 실행 (Haiku, tool_use 구조화)."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    system = f"""당신은 '{persona_name}' 관점의 투자 분석가입니다.
{persona_prompt}

시장 데이터를 분석한 후 반드시 submit_analysis 도구를 호출하여 결과를 제출하세요."""

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": market_context}],
            tools=[_ANALYSIS_TOOL],
            tool_choice={"type": "tool", "name": "submit_analysis"},
        )

        # tool_use 블록에서 구조화된 데이터 추출
        data: dict = {}
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_analysis":
                data = block.input
                break

        if not data:
            raise ValueError("tool_use 응답 없음")

        return PersonaAnalysis(
            persona=persona_name,
            verdict=data.get("verdict", "관망"),
            confidence=int(data.get("confidence", 50)),
            reasoning=data.get("reasoning", ""),
            key_factors=tuple(data.get("key_factors", [])),
            risk_warning=data.get("risk_warning", ""),
        )
    except Exception as e:
        log.warning(f"페르소나 분석 실패 ({persona_name}): {e}")
        return PersonaAnalysis(
            persona=persona_name,
            verdict="관망",
            confidence=0,
            reasoning=f"분석 실패: {e}",
        )


def run_all_personas(market_context: str) -> list[PersonaAnalysis]:
    """4개 페르소나를 병렬 실행 + 의견 충돌 시 반론 라운드.

    1라운드: 4명 독립 분석 (병렬)
    2라운드: 의견 충돌 감지 시 반론 (병렬) -- 다른 페르소나의 판단을 보고 재판단

    Returns:
        PersonaAnalysis 리스트 (반론 라운드 후 최종 판단)
    """
    # 1라운드: 독립 분석
    results: list[PersonaAnalysis] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_run_persona, name, prompt, market_context): name
            for name, prompt in PERSONAS.items()
        }
        for future in as_completed(futures):
            results.append(future.result())

    # 충돌 감지: 매수 vs 매도 의견이 동시에 존재하면 반론 라운드
    verdicts = {r.verdict for r in results if r.confidence > 0}
    has_buy = "매수" in verdicts
    has_sell = "매도" in verdicts
    high_conf_spread = max((r.confidence for r in results), default=0) - min(
        (r.confidence for r in results if r.confidence > 0), default=0
    )

    if not (has_buy and has_sell) and high_conf_spread < 30:
        return results  # 충돌 없음 -- 반론 불필요

    log.info("  의견 충돌 감지 -- 반론 라운드 시작")

    # 2라운드: 다른 페르소나의 판단을 보고 재판단
    debate_context = _build_debate_context(results)
    augmented_context = f"{market_context}\n\n{debate_context}"

    round2: list[PersonaAnalysis] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                _run_persona, name, prompt, augmented_context
            ): name
            for name, prompt in PERSONAS.items()
        }
        for future in as_completed(futures):
            round2.append(future.result())

    return round2


def _build_debate_context(results: list[PersonaAnalysis]) -> str:
    """반론 라운드용 컨텍스트 -- 다른 분석가들의 의견 요약."""
    lines = [
        "━━━ 다른 분석가들의 의견 (1라운드 결과) ━━━",
        "아래 의견을 검토하고, 동의하거나 반론하세요.",
        "약한 논리가 있으면 지적하고, 자신의 판단을 수정하거나 더 강하게 유지하세요.",
        "",
    ]
    for r in results:
        factors = ", ".join(r.key_factors) if r.key_factors else "-"
        lines.append(
            f"[{r.persona}] {r.verdict} (확신도 {r.confidence}%): "
            f"{r.reasoning} | 리스크: {r.risk_warning}"
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# 종합 판단
# ═══════════════════════════════════════════════════════
def synthesize(
    persona_results: list[PersonaAnalysis],
    market_context: str,
    briefing_type: str = "MANUAL",
) -> str:
    """4개 페르소나 분석을 종합하여 최종 전략 JSON 생성 (Sonnet)."""
    # 페르소나 결과 텍스트화
    persona_text = ""
    for pa in persona_results:
        factors = ", ".join(pa.key_factors) if pa.key_factors else "-"
        persona_text += f"""
【{pa.persona}】 판단: {pa.verdict} (확신도: {pa.confidence}%)
  근거: {pa.reasoning}
  핵심 요인: {factors}
  리스크: {pa.risk_warning}
"""

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    # 시장 초점 지시
    if briefing_type == "KR_BEFORE":
        market_focus = """
⑥ 이 브리핑은 【한국 시장 중심】입니다. 한국 종목(삼성전자, 한화에어로스페이스, 국내 ETF 등)에 초점을 맞추세요.
⑦ 코스피/코스닥 동향, 외국인/기관 수급, 원달러 환율이 핵심입니다.
⑧ 미국 시장은 한국장에 미치는 영향 관점에서만 간략히 언급하세요."""
    elif briefing_type == "US_BEFORE":
        market_focus = """
⑥ 이 브리핑은 【미국 시장 중심】입니다. 미국 종목(NVDA, GOOGL, MU, LMT)에 초점을 맞추세요.
⑦ S&P500/나스닥/다우 동향, Fed 정책, VIX, 미국 국채 금리가 핵심입니다.
⑧ 한국 시장은 미국장에 미치는 영향 관점에서만 간략히 언급하세요."""
    else:
        market_focus = ""

    # 보유 포지션 정보 (계좌별, briefing_type에 따라 필터링)
    from config.settings import (
        DEFAULT_CASH,
        HOLDINGS_GENERAL,
        HOLDINGS_ISA,
        HOLDINGS_IRP,
        HOLDINGS_PENSION,
        ISA_CASH,
        IRP_CASH,
        IRP_DEFAULT_OPTION,
        PENSION_MMF,
    )

    def _is_kr_ticker(tk: str) -> bool:
        return ".KS" in tk

    def _filter_holdings(holdings: dict, bt: str) -> dict:
        if bt == "KR_BEFORE":
            return {tk: info for tk, info in holdings.items() if _is_kr_ticker(tk)}
        if bt == "US_BEFORE":
            return {tk: info for tk, info in holdings.items() if not _is_kr_ticker(tk)}
        return holdings

    def _fmt_holding(tk: str, info: dict) -> str:
        shares = info.get("shares", 0)
        ria = info.get("ria_eligible", 0)
        ria_tag = f" [RIA 적격 {ria}주]" if ria > 0 else ""
        if "avg_cost_usd" in info:
            return f"  {tk}: {shares}주 (매수 ${info['avg_cost_usd']:.2f}){ria_tag}"
        return f"  {tk}: {shares}주 (매수 ₩{info.get('avg_cost_krw', 0):,.0f})"

    filtered_general = _filter_holdings(HOLDINGS_GENERAL, briefing_type)
    filtered_isa = _filter_holdings(HOLDINGS_ISA, briefing_type)
    filtered_irp = _filter_holdings(HOLDINGS_IRP, briefing_type)
    filtered_pension = _filter_holdings(HOLDINGS_PENSION, briefing_type)

    general_lines = [_fmt_holding(tk, info) for tk, info in filtered_general.items()]
    isa_lines = [_fmt_holding(tk, info) for tk, info in filtered_isa.items()]
    irp_lines = [_fmt_holding(tk, info) for tk, info in filtered_irp.items()]
    pension_lines = [_fmt_holding(tk, info) for tk, info in filtered_pension.items()]

    holdings_text = f"""[일반] 종합계좌 (예수금 ₩{DEFAULT_CASH:,.0f})
{chr(10).join(general_lines) if general_lines else "  (해당 시장 보유 없음)"}

[ISA] 중개형 ISA (예수금 ₩{ISA_CASH:,.0f})
{chr(10).join(isa_lines) if isa_lines else "  (보유 종목 없음 — 신규 매수 가능)"}

[IRP] 퇴직연금 (현금 ₩{IRP_CASH:,.0f} + 디폴트옵션 ₩{IRP_DEFAULT_OPTION:,.0f})
{chr(10).join(irp_lines) if irp_lines else "  (해당 시장 보유 없음)"}

[연금저축] CMA (MMF ₩{PENSION_MMF:,.0f})
{chr(10).join(pension_lines) if pension_lines else "  (해당 시장 보유 없음)"}"""

    system = f"""당신은 최고 투자 전략가(CIO)입니다. 4명의 분석가 의견을 종합하여 최종 전략을 결정합니다.

규칙:
① 다수결이 아닌 논리적 종합 판단 — 확신도가 높은 분석가의 의견에 가중치
② 리스크 경고가 중복되면 심각하게 반영
③ 분석가 간 의견 충돌이 있으면 명시
④ 아부 금지. 데이터 기반 직언.
⑤ 모든 수치는 구체적으로 (%, 가격)

━━━ 실제 보유 포지션 ━━━
{holdings_text}
예수금: ₩{DEFAULT_CASH:,.0f}

━━━ 계좌 규칙 (반드시 준수) ━━━
- 모든 매수/매도 신호에 계좌 태그 필수: [일반], [ISA], [RIA], [연금저축], [IRP]
- [ISA]: 국내주식 + 국내상장 ETF만 매수 가능. 해외 개별주식 불가. 예수금 2,000만원 — 적극 활용 대상.
- [RIA]: 매도 전용. NVDA/GOOGL만 적격 (2025.12.23 이전 매수분). 5/31까지 100% 양도세 면제.
- [일반]: 5/31 전까지 해외주식 신규 매수 금지 (RIA 한도 차감). 국내주식은 ISA 우선 매수.
- [연금저축/IRP]: 2026년 납입 완료. 리밸런싱만.
- 전문 용어 사용 시 괄호로 쉬운 설명 병기 (예: RSI(과매도 지표) 35)
- 한눈에 알아보기 쉽게. 표 적극 활용. 결론 먼저.{market_focus}"""

    prompt = f"""{market_context}

━━━ 4명의 분석가 의견 ━━━
{persona_text}

위 분석을 종합하여 아래 JSON을 생성하세요 (코드블록 없이):
{{
  "title": "날짜+시간 + 핵심 요약",
  "market_status": "상승|하락|보합|혼조",
  "investment_decision": "매수실행|매도실행|보류|관망",
  "market_summary": "리스크 먼저, 수치 중심, 400자 이상. 4개 관점 종합.",
  "consensus": "4명 합의점 요약",
  "dissent": "의견 불일치 지점",
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
      "account": "[ISA]|[일반]|[RIA]",
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
      "account": "[일반]|[RIA]",
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
  "advisor_conclusion": "300자 이상 종합 결론. 4개 관점의 합의/불일치 반영.",
  "advisor_checklist": [
    {{"condition": "조건", "status": "충족|미충족|부분충족", "detail": "현황"}}
  ],
  "advisor_risks": ["리스크 1", "리스크 2"],
  "advisor_opportunities": ["기회 1", "기회 2"],
  "advisor_scenarios": [
    {{"label": "시나리오", "condition": "조건", "action": "액션", "amount": "금액"}}
  ],
  "next_action": "다음 액션",
  "account_strategy": {{
    "ISA": "국내 ETF/주식 매수 전략 또는 대기 사유",
    "RIA": "NVDA/GOOGL 매도 판단 (5/31 데드라인)",
    "일반": "해외주식 전략 (5/31 전 매수 금지 명시)",
    "연금_IRP": "리밸런싱 사항 또는 변동 없음"
  }},
  "persona_summary": {{
    "가치투자자": "한줄 요약",
    "성장투자자": "한줄 요약",
    "기술적분석가": "한줄 요약",
    "매크로분석가": "한줄 요약"
  }}
}}"""

    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=10000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
