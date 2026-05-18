"""
멀티 에이전트 분석 — 4개 투자자 페르소나 + 종합 판단

ai-hedge-fund 패턴 참고: 각 페르소나가 독립 분석 후
종합 에이전트가 최종 매매 판단을 내린다.

페르소나 에이전트: Haiku 4.5 OAuth (tool_use 구조화 + 병렬)
종합 에이전트: Opus CLI ($0) → OAuth API 폴백 (Opus → Sonnet → Haiku)
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

import anthropic

from config.settings import KST
from core.claude_cli import claude_cli
from datetime import date as _date
from core.recovery import claude_breaker, retry
from core.task_registry import get_registry

log = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"
OPUS_MODEL = "claude-opus-4-7"


def _extract_persona_data(text: str) -> dict:
    """페르소나 응답 텍스트를 PersonaAnalysis dict로 파싱한다.

    1) JSON 통째로 → ```json``` 블록 → 첫 { ~ 마지막 } 시도
    2) 실패 시 자연어에서 verdict/confidence 정규식 추출
    """
    text = text.strip()
    import re

    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "verdict" in data:
                return data
        except json.JSONDecodeError:
            continue

    verdict_match = re.search(r"(매수|매도|홀딩|관망)", text)
    confidence_match = (
        re.search(r"확신도[^\d]{0,5}(\d{1,3})", text)
        or re.search(r"(\d{1,3})\s*[%／/]\s*100", text)
        or re.search(r"\b(\d{1,3})\s*%", text)
    )

    if not verdict_match:
        raise ValueError(f"verdict 추출 실패 (미리보기: {text[:200]!r})")

    return {
        "verdict": verdict_match.group(1),
        "confidence": min(100, max(0, int(confidence_match.group(1)))) if confidence_match else 50,
        "reasoning": text[:600],
        "key_factors": [],
        "risk_warning": "",
    }


def _json_candidates(text: str):
    """가능한 JSON 후보 substrings를 yield."""
    import re
    yield text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        yield fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        yield text[start : end + 1]


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
    stock_views: tuple[dict, ...] = ()  # [{ticker, view, reason}, ...]


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
                "description": "핵심 판단 근거. 구체 종목·수치·사례 포함 500~800자. 단순 결론 금지, 추론 과정 명시.",
            },
            "key_factors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "핵심 요인 5개. 각 항목은 종목/지표 + 수치 + 시사점 형태",
            },
            "risk_warning": {
                "type": "string",
                "description": "주요 리스크. 구체 시나리오 + 트리거 + 대응책 포함 200~300자",
            },
            "stock_views": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "view": {"type": "string", "enum": ["매수", "매도", "홀딩", "관망"]},
                        "reason": {"type": "string", "description": "100자 이내"},
                    },
                    "required": ["ticker", "view", "reason"],
                },
                "description": "포트폴리오 종목별 개별 의견 (선택적, 있으면 3~5개)",
            },
        },
        "required": ["verdict", "confidence", "reasoning", "key_factors", "risk_warning"],
    },
}


def _run_persona_gemini(
    persona_name: str,
    persona_prompt: str,
    market_context: str,
    system: str,
) -> dict:
    """Gemini API로 페르소나 분석 (JSON mode 강제). 실패 시 RuntimeError."""
    from core.recovery import gemini_breaker

    if not gemini_breaker.is_available:
        raise RuntimeError("Gemini API 서킷 브레이커 OPEN")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 미설정")

    from google import genai
    from google.genai import types

    schema_for_gemini = _ANALYSIS_TOOL["input_schema"]

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[market_context],
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=2000,
            response_mime_type="application/json",
            response_schema=schema_for_gemini,
        ),
    )
    raw = response.text.strip() if response.text else ""
    if not raw:
        gemini_breaker.record_failure()
        raise RuntimeError("Gemini 응답 비어있음")

    gemini_breaker.record_success()
    return _extract_persona_data(raw)


def _persona_backend(persona_name: str) -> str:
    """페르소나 → 백엔드 라우팅. 환경 변수로 Gemini 하이브리드 토글."""
    if os.environ.get("STOCK_PERSONA_GEMINI", "false").lower() != "true":
        return "claude"
    if persona_name in ("성장투자자", "기술적분석가"):
        return "gemini"
    return "claude"


def _run_persona(
    persona_name: str,
    persona_prompt: str,
    market_context: str,
    team_id: str = "",
) -> PersonaAnalysis:
    """단일 페르소나 분석 실행 (Haiku, tool_use 구조화).

    Task 레지스트리로 상태 추적 + 서킷 브레이커 적용.
    """
    registry = get_registry()
    task = registry.create_task(persona_name, "persona", team_id=team_id)
    registry.start_task(task.task_id)

    schema_for_prompt = json.dumps(_ANALYSIS_TOOL["input_schema"], ensure_ascii=False, indent=2)
    system = f"""당신은 '{persona_name}' 관점의 투자 분석가입니다.
{persona_prompt}

분석 대상:
- 보유 종목(포트폴리오): 매수/매도/홀딩/관망 판단
- **신규 매수 후보(Watchlist)**: 시장 컨텍스트에 별도 섹션으로 제공됨. 보유 외 종목 중 매수 매력이 있다고 판단되면 stock_views 또는 reasoning에 명시할 것.

응답 규칙 (반드시 준수):
1. 응답은 단일 JSON 객체만 포함합니다. 마크다운 헤더, 표, 설명 텍스트 금지.
2. 응답의 첫 글자는 `{{`, 마지막 글자는 `}}`입니다. 코드 펜스(```) 사용 금지.
3. 다음 스키마를 정확히 따릅니다:
{schema_for_prompt}

응답 예시:
{{"verdict":"매수","confidence":75,"reasoning":"한화에어로 RSI 29.1 과매도, 매출 +74.5%. Watchlist의 시프트업도 RSI 35로 진입 검토 가능","key_factors":["방산 TAM 확장","RSI 과매도","외인 매도 마무리","시프트업 신규 진입 후보","ETF 과열 회피"],"risk_warning":"코스피 사상최고 부담","stock_views":[{{"ticker":"012450.KS","view":"매수","reason":"보유, RSI 29 분할매수"}},{{"ticker":"462870.KS","view":"매수","reason":"신규 후보, RSI 35 진입"}}]}}"""

    try:
        backend = _persona_backend(persona_name)
        if backend == "gemini":
            try:
                data = _run_persona_gemini(
                    persona_name, persona_prompt, market_context, system,
                )
                claude_breaker.record_success()
                result = PersonaAnalysis(
                    persona=persona_name,
                    verdict=data.get("verdict", "관망"),
                    confidence=int(data.get("confidence", 50)),
                    reasoning=data.get("reasoning", ""),
                    key_factors=tuple(data.get("key_factors", [])),
                    risk_warning=data.get("risk_warning", ""),
                    stock_views=tuple(data.get("stock_views", []) or []),
                )
                registry.complete_task(task.task_id, result)
                return result
            except Exception as ge:
                log.warning(f"Gemini 페르소나 실패 ({persona_name}): {ge} → Claude 폴백")

        if not claude_breaker.is_available:
            raise RuntimeError("Claude CLI 서킷 브레이커 OPEN")

        schema_json = json.dumps(_ANALYSIS_TOOL["input_schema"], ensure_ascii=False)
        raw = ""
        for attempt, model in enumerate(("sonnet", "sonnet", "haiku")):
            if attempt > 0:
                import time as _time
                _time.sleep(3 * attempt)
                log.info(
                    "페르소나 재시도 (%s, attempt=%d, model=%s)",
                    persona_name, attempt + 1, model,
                )
            raw = claude_cli(
                prompt=market_context,
                model=model,
                system_prompt=system,
                timeout=180,
                json_schema=schema_json,
            )
            if raw:
                break

        if not raw:
            raise RuntimeError("Claude CLI 응답 없음 (3회 재시도 모두 실패)")

        data = _extract_persona_data(raw)

        claude_breaker.record_success()

        result = PersonaAnalysis(
            persona=persona_name,
            verdict=data.get("verdict", "관망"),
            confidence=int(data.get("confidence", 50)),
            reasoning=data.get("reasoning", ""),
            key_factors=tuple(data.get("key_factors", [])),
            risk_warning=data.get("risk_warning", ""),
        )
        registry.complete_task(task.task_id, result)
        return result
    except Exception as e:
        claude_breaker.record_failure()
        registry.fail_task(task.task_id, str(e))
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

    Task 레지스트리로 팀 단위 실행 추적.

    Returns:
        PersonaAnalysis 리스트 (반론 라운드 후 최종 판단)
    """
    registry = get_registry()
    team = registry.create_team("persona_round1")
    registry.start_team(team.team_id)

    # 1라운드: 독립 분석
    results: list[PersonaAnalysis] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {
            executor.submit(
                _run_persona, name, prompt, market_context, team.team_id,
            ): name
            for name, prompt in PERSONAS.items()
        }
        for future in as_completed(futures):
            results.append(future.result())

    registry.complete_team(team.team_id)

    # 실행 요약 로깅
    summary = registry.get_team_summary(team.team_id)
    log.info(
        "  라운드1 완료: %d/%d 성공 (%.1fs)",
        summary.get("completed", 0), summary.get("total", 0),
        summary.get("duration_sec", 0),
    )

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

    # 2라운드: 팀 생성 + 반론
    team2 = registry.create_team("persona_round2_debate")
    registry.start_team(team2.team_id)

    debate_context = _build_debate_context(results)
    augmented_context = f"{market_context}\n\n{debate_context}"

    round2: list[PersonaAnalysis] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {
            executor.submit(
                _run_persona, name, prompt, augmented_context, team2.team_id,
            ): name
            for name, prompt in PERSONAS.items()
        }
        for future in as_completed(futures):
            round2.append(future.result())

    registry.complete_team(team2.team_id)

    summary2 = registry.get_team_summary(team2.team_id)
    log.info(
        "  라운드2 완료: %d/%d 성공 (%.1fs)",
        summary2.get("completed", 0), summary2.get("total", 0),
        summary2.get("duration_sec", 0),
    )

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
    """4개 페르소나 분석을 종합하여 최종 전략 JSON 생성.

    1순위: Opus CLI (Max 구독, $0)
    2순위: Sonnet CLI (Max 구독, $0) — Opus 타임아웃 시 폴백
    """
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

    # 보유 포지션 정보 (계좌별, briefing_type에 따라 필터링)
    from config.settings import (
        DEFAULT_CASH,
        HOLDINGS_GENERAL,
        HOLDINGS_ISA,
        HOLDINGS_IRP,
        HOLDINGS_PENSION,
        HOLDINGS_RIA,
        ISA_CASH,
        IRP_CASH,
        IRP_DEFAULT_OPTION,
        PENSION_MMF,
        RIA_CASH,
        RIA_REALIZED_GAIN_USD,
    )

    # RIA 현재 상태 (프롬프트 동적 갱신용)
    ria_nvda_shares = HOLDINGS_RIA.get("NVDA", {}).get("shares", 0)
    ria_nvda_avg = HOLDINGS_RIA.get("NVDA", {}).get("avg_cost_usd", 0.0)

    # RIA 외 해외 매수 가중치 (사용자 B안 전략 기반)
    _today = _date.today()
    # 절세 손실 계산: (매수금액 × 가중치 / RIA 매도금액) × 양도차익 × 세율
    # 현재 상태: RIA 매도금액 ₩20,584,797, 양도차익 ₩7,210,348, 세율 22%
    _ria_sales_krw = 20_584_797
    _ria_gain_krw = 7_210_348
    _tax_rate = 0.22

    if _today >= _date(2027, 1, 1):
        ria_weight_phase = "종료(2027+)"
        ria_weight_pct = 0
        ria_tax_loss_per_10m = 0
        ria_b_guidance = "RIA 제도 종료. 일반/ISA/IRP/연금에서 해외 ETF·해외주식 자유 매수 추천 (절세 손실 없음)."
    elif _today >= _date(2026, 8, 1):
        ria_weight_phase = "8~12월"
        ria_weight_pct = 50
        ria_tax_loss_per_10m = int(10_000_000 * 0.50 / _ria_sales_krw * _ria_gain_krw * _tax_rate)
        ria_b_guidance = f"가중치 50% 적용 시점. ₩10,000,000 매수 시 절세 손실 약 ₩{ria_tax_loss_per_10m:,}. B안 권장 시점 — 매수 적극 검토."
    elif _today >= _date(2026, 6, 1):
        ria_weight_phase = "6~7월"
        ria_weight_pct = 80
        ria_tax_loss_per_10m = int(10_000_000 * 0.80 / _ria_sales_krw * _ria_gain_krw * _tax_rate)
        ria_b_guidance = f"가중치 80% 적용. ₩10,000,000 매수 시 절세 손실 약 ₩{ria_tax_loss_per_10m:,}. B안 기본은 자제이나 강세 신호 명확 시 오버라이드 가능 — 기대 수익이 절세 손실 초과 시 매수 추천."
    else:
        ria_weight_phase = "5월 이전 (현재)"
        ria_weight_pct = 100
        ria_tax_loss_per_10m = int(10_000_000 * 1.00 / _ria_sales_krw * _ria_gain_krw * _tax_rate)
        ria_b_guidance = f"가중치 100% 적용. ₩10,000,000 매수 시 절세 손실 약 ₩{ria_tax_loss_per_10m:,}. 매우 강한 강세 신호 아니면 자제 권장."

    # 시장 초점 지시
    if briefing_type == "KR_BEFORE":
        market_focus = """
⑥ 이 브리핑은 【한국 시장 중심】입니다. 한국 종목(삼성전자, 한화에어로스페이스, 국내 ETF 등)에 초점을 맞추세요.
⑦ 코스피/코스닥 동향, 외국인/기관 수급, 원달러 환율이 핵심입니다.
⑧ 미국 시장은 한국장에 미치는 영향 관점에서만 간략히 언급하세요."""
    elif briefing_type == "US_BEFORE":
        us_focus_tickers = "MU, LMT" if ria_nvda_shares == 0 else "NVDA, MU, LMT"
        if ria_nvda_shares > 0:
            ria_focus = (
                f"⑨ **RIA NVDA {ria_nvda_shares}주(평단 ${ria_nvda_avg:.2f})는 5/31 100% 면제 데드라인 종목 "
                f"— 매 브리핑마다 매도 타이밍 명시 필수. 누적 면제 차익 ${RIA_REALIZED_GAIN_USD:,.2f} 사용.**"
            )
        else:
            ria_focus = (
                f"⑨ RIA 잔존 0, 누적 면제 차익 ${RIA_REALIZED_GAIN_USD:,.2f} 확정. RIA 현금 ₩{RIA_CASH:,.0f} 활용처는 국내자산 편입 ETF·국내주식 위주 추천 (해외편입 ETF 불가, 1년 의무 보유).\n"
                f"⑩ 일반/ISA에서 미국주·해외 ETF 매수 추천 시 B안 룰: 현재 가중치 {ria_weight_pct}% ({ria_weight_phase}). "
                f"강세 신호 명확(VIX <18 + RSI 50+ + 5일+ 상승 추세, 또는 종목별 강력 매수 시그널)이면 절세 손실 약 ₩{ria_tax_loss_per_10m:,}/₩10M 명시 후 오버라이드 추천 OK. 모호하면 매수 자제."
            )
        market_focus = f"""
⑥ 이 브리핑은 【미국 시장 중심】입니다. 미국 종목({us_focus_tickers})에 초점을 맞추세요.
⑦ S&P500/나스닥/다우 동향, Fed 정책, VIX, 미국 국채 금리가 핵심입니다.
⑧ 한국 시장은 미국장에 미치는 영향 관점에서만 간략히 언급하세요.
{ria_focus}"""
    else:
        market_focus = ""

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
    filtered_ria = _filter_holdings(HOLDINGS_RIA, briefing_type)
    filtered_isa = _filter_holdings(HOLDINGS_ISA, briefing_type)
    filtered_irp = _filter_holdings(HOLDINGS_IRP, briefing_type)
    filtered_pension = _filter_holdings(HOLDINGS_PENSION, briefing_type)

    general_lines = [_fmt_holding(tk, info) for tk, info in filtered_general.items()]
    ria_lines = [_fmt_holding(tk, info) for tk, info in filtered_ria.items()]
    isa_lines = [_fmt_holding(tk, info) for tk, info in filtered_isa.items()]
    irp_lines = [_fmt_holding(tk, info) for tk, info in filtered_irp.items()]
    pension_lines = [_fmt_holding(tk, info) for tk, info in filtered_pension.items()]

    holdings_text = f"""[일반] 종합계좌 (예수금 ₩{DEFAULT_CASH:,.0f})
{chr(10).join(general_lines) if general_lines else "  (해당 시장 보유 없음)"}

[RIA] 종합(RIA) (예수금 ₩{RIA_CASH:,.0f}) — 5/31까지 100% 양도세 면제
{chr(10).join(ria_lines) if ria_lines else "  (해당 시장 보유 없음)"}

[ISA] 중개형 ISA (예수금 ₩{ISA_CASH:,.0f})
{chr(10).join(isa_lines) if isa_lines else "  (보유 종목 없음 — 신규 매수 가능)"}

[IRP] 퇴직연금 (현금 ₩{IRP_CASH:,.0f} + 디폴트옵션 ₩{IRP_DEFAULT_OPTION:,.0f})
{chr(10).join(irp_lines) if irp_lines else "  (해당 시장 보유 없음)"}

[연금저축] CMA (MMF ₩{PENSION_MMF:,.0f})
{chr(10).join(pension_lines) if pension_lines else "  (해당 시장 보유 없음)"}"""

    ria_days_left = max((_date(2026, 5, 31) - _date.today()).days, 0)

    system = f"""당신은 최고 투자 전략가(CIO)입니다. 4명의 분석가 의견을 종합하여 최종 전략을 결정합니다.

규칙:
① 다수결이 아닌 논리적 종합 판단 — 확신도가 높은 분석가의 의견에 가중치
② 리스크 경고가 중복되면 심각하게 반영
③ 분석가 간 의견 충돌이 있으면 명시
④ 아부 금지. 데이터 기반 직언.
⑤ 모든 수치는 구체적으로 (%, 가격)
⑥ 관망도 적극적 판단이다 — "살 수 없으니 관망"은 금지. 제약 내에서 최선의 액션을 찾아라.
   ISA에서 국내 ETF/주식 매수 기회가 있는지, RIA 매도 타이밍이 맞는지, 리밸런싱 필요성이 있는지 반드시 검토.
   진짜 할 게 없으면 "왜 지금은 안 되는지" 구체적 조건과 "어떤 조건이 충족되면 행동할지" 트리거를 명시.
⑦ **strategy_buy / buy_recommendations에는 보유 종목뿐 아니라 Watchlist 신규 후보도 포함**할 것. 시장 컨텍스트에 별도 'Watchlist' 섹션이 있으며, RSI/MA 기준으로 매력적인 종목이 있으면 매수 후보로 명시 추천. 기존 포트폴리오에 없는 종목이라도 진입가/손절/익절을 구체화.

━━━ 실제 보유 포지션 ━━━
{holdings_text}
예수금: ₩{DEFAULT_CASH:,.0f}

━━━ 계좌 규칙 (반드시 준수) ━━━
- 모든 매수/매도 신호에 계좌 태그 필수: [일반], [ISA], [RIA], [연금저축], [IRP]
- [ISA]: 국내주식 + 국내상장 ETF 매수 가능. 해외 개별주식 불가. 예수금 ₩{ISA_CASH:,.0f}.
- [RIA]: {("매도 전용. 현재 잔존 NVDA " + str(ria_nvda_shares) + "주만 보유. 5/31까지 잔여분 100% 양도세 면제.") if ria_nvda_shares > 0 else "현금 ₩" + f'{RIA_CASH:,.0f}' + " 1년 의무 보유 (인출 시 전체 면제 취소). 국내자산 편입 ETF·국내주식·예탁금만 매수 가능 — 해외편입 ETF(TIGER 미국 시리즈·KODEX MSCI선진국·TIGER 차이나 등) 매수 불가."} 누적 실현 차익 ${RIA_REALIZED_GAIN_USD:,.2f}.
- [일반]: 예수금 ₩{DEFAULT_CASH:,.0f}. 한국·미국·ETF 자유 매수 가능. 단 미국주·해외 ETF 매수는 사용자 B안 룰 적용 (아래 참조).
- [IRP/연금저축]: 해외 ETF 자동매수 설정 있다면 정지 권장 (RIA 혜택 보호용, B안 룰).

━━━ 🎯 사용자 B안 전략 (2026-05-18 결정) ━━━
- **기본 방침**: 2026-08-01 전까지 일반·ISA·IRP·연금저축에서 해외 ETF·해외주식·해외주식형 펀드 신규 매수 자제 (RIA 세제혜택 보호)
- **현재 시점**: {ria_weight_phase} (가중치 {ria_weight_pct}%). {ria_b_guidance}
- **오버라이드 조건** (강세 시 매수 추천 허용):
  · 명확한 강세 신호 1: VIX < 18 + S&P500/나스닥 RSI 50+ + 5일+ 상승 추세
  · 명확한 강세 신호 2: 종목별 강력 매수 시그널 (RSI 30 이하 과매도 반등 + 거래량 급증 + 핵심 지지선 확인 등)
  · 오버라이드 추천 시 절세 손실액 정량 명시 필수: 현재 가중치 기준 ₩10,000,000 매수당 약 ₩{ria_tax_loss_per_10m:,} 절세 손실
  · 기대 수익 vs 절세 손실 정량 비교 후 추천 (예: "절세 손실 ₩{ria_tax_loss_per_10m:,} vs 단기 +5% 기대 = ₩500,000 수익 → 매수 합리")
- **계좌별 추천 가이드**:
  · [RIA]: 국내자산 편입 ETF 위주 매수 추천 (KODEX 200·KODEX 코스닥150·TIGER 200·KODEX 자동차·KODEX 반도체·PLUS 고배당주 등). 진입가·분할 계획·트리거 명시. 1년 의무 보유 감안 장기 종목 선호.
  · [일반]·[ISA]: 한국 자산은 자유 추천 (RIA 영향 없음). 미국 자산은 B안 + 오버라이드 룰 적용.
  · [IRP]·[연금저축]: 한국 자산 위주 리밸런싱 추천. 해외 ETF 신규는 B안 영향 표시.

━━━ RIA 진행 상황 ━━━
- 5/31 면제 데드라인: D-{(_date(2026, 5, 31) - _date.today()).days}일
- 잔존: {("NVDA " + str(ria_nvda_shares) + "주 (평단 $" + f"{ria_nvda_avg:.2f}" + ")") if ria_nvda_shares > 0 else "0 — 전량 매도 완료 (2026-05-18)"}
- 누적 실현 면제 차익: ${RIA_REALIZED_GAIN_USD:,.2f} (1차 5/12 GOOGL 9 @$387 + NVDA 23 @$219, 2차 5/14 NVDA 12 @$232, 3차 5/18 NVDA 11 @$228.30)
{("- 잔여 NVDA " + str(ria_nvda_shares) + "주도 100% 양도세 면제 → 반드시 5/31 전 매도 완료. 기술적 과열(RSI 70+) 즉시 매도, D-10 이내 강제 매도.") if ria_nvda_shares > 0 else "- RIA 매도 타이밍 분석 불필요. RIA 현금 ₩{0:,.0f} 활용처(일반/ISA 이체·재투자) 위주로 검토.".format(RIA_CASH)}
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
  "strategy_summary": "오늘 가장 중요한 매수/매도 판단 요약. 보유+신규 후보 모두 다룰 것. 400자 이상.",
  "advisor_verdict": "적극매수|소액분할|매도고려|리밸런싱|매수대기",
  "advisor_oneliner": "한 문장 직언 (수치 포함)",
  "advisor_conclusion": "500자 이상 종합 결론. 4개 관점의 합의/불일치 반영. 보유 종목과 Watchlist 신규 후보를 모두 검토.",
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
    "ISA": "한국주식·국내 ETF 구체 매수 후보·진입가·분할 계획·손절가 명시. 해외 ETF(TIGER 미국/KODEX MSCI선진국 등) 추천 시 B안 룰 적용 — 현재 가중치 {ria_weight_pct}%, 강세 신호 명확하면 절세 손실 정량 명시 후 오버라이드 가능. 한국 자산은 자유 추천.",
    "RIA": "{('잔존 NVDA ' + str(ria_nvda_shares) + '주 매도 타이밍 명시 (RSI/추세/D-' + str(ria_days_left) + '). 5/31 면제 ₩5,000만 한도 대비 누적 $' + f'{RIA_REALIZED_GAIN_USD:,.2f}' + ' 사용.') if ria_nvda_shares > 0 else ('현금 ₩' + f'{RIA_CASH:,.0f}' + ' 활용처 — 국내자산 편입 ETF 위주 매수 추천 (KODEX 200/코스닥150/TIGER 200/KODEX 반도체·자동차·PLUS 고배당주 등). 종목별 진입가·분할 계획·매수 트리거(예: RSI 40 이하 진입, MA60 지지 등) 명시 필수. 해외편입 ETF·미국 개별주 추천 절대 금지. 1년 의무 보유(2027-05-12/14/18 분할 만료) 감안 장기 종목 선호. 예탁금 보유 대안도 비교 제시. 누적 실현 면제 $' + f'{RIA_REALIZED_GAIN_USD:,.2f}' + ' 확정.')}",
    "일반": "예수금 ₩{DEFAULT_CASH:,.0f}. 한국주식·한국 ETF 자유 추천. 미국주식·해외 ETF는 B안 룰 적용 — 현재 가중치 {ria_weight_pct}% ({ria_weight_phase}). 매수 추천 시 'RIA 절세 손실 ₩X vs 단기 기대 수익 ₩Y' 정량 비교 후 추천/보류 결정. 기존 보유 MU/LMT 관리 전략도 포함.",
    "연금_IRP": "한국 자산 리밸런싱 위주 추천. 해외 ETF(TIGER 미국 시리즈·차이나·KODEX MSCI선진국) 자동매수 설정 있다면 정지 권장 명시. 신규 매수 시 B안 룰 + 가중치 {ria_weight_pct}% 영향 표시."
  }},
  "persona_summary": {{
    "가치투자자": "한줄 요약",
    "성장투자자": "한줄 요약",
    "기술적분석가": "한줄 요약",
    "매크로분석가": "한줄 요약"
  }}
}}"""

    import time as _time
    attempts = (
        ("opus", 600, "medium"),
        ("opus", 600, "medium"),
        ("sonnet", 300, "medium"),
        ("sonnet", 300, "medium"),
        ("haiku", 240, "low"),
    )

    for idx, (model, timeout, effort) in enumerate(attempts):
        if idx > 0:
            wait = 5 * idx
            log.warning(
                "synthesis: %d번째 시도 → %s (대기 %ds)", idx + 1, model, wait,
            )
            _time.sleep(wait)
        cli_output = claude_cli(
            prompt,
            model=model,
            system_prompt=system,
            timeout=timeout,
            effort=effort,
        )
        if cli_output:
            log.info(
                "synthesis: %s CLI 성공 (%d chars, attempt=%d)",
                model, len(cli_output), idx + 1,
            )
            return cli_output

    raise RuntimeError("synthesis: 모든 모델 retry 실패")
