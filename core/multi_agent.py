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
        for attempt, model in enumerate(("opus", "sonnet", "haiku")):
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
                timeout=300,
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
    with ThreadPoolExecutor(max_workers=2) as executor:
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
    with ThreadPoolExecutor(max_workers=2) as executor:
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
    current_prices: dict[str, float] | None = None,
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
    if briefing_type == "KR_NIGHT":
        market_focus = """
⑥ 이 브리핑은 【한국 시장 야간 프리브리핑】입니다. **내일 장 시작 전 지정가 예약 주문**을 위한 사전 분석입니다.
⑦ 코스피/코스닥 동향, 외국인/기관 수급, 원달러 환율, 오늘 미국장 마감 영향이 핵심입니다.
⑧ 미국장 마감 데이터가 내일 한국장 갭 오프닝에 미칠 영향을 반드시 분석하세요.

━━━ 🌙 야간 프리브리핑 특화 규칙 ━━━

⚠️ **night_orders = 브리핑 받자마자 HTS/MTS에 바로 입력할 주문 (가장 중요)**:
- night_orders는 "분석"이 아니라 **"지금 당장 실행할 주문"**이다.
- 현재가 기준으로 **내일 장 시작 시 체결 가능한 가격**만 지정가로 넣어라.
- 현재가와 동떨어진 가격(현재가 대비 ±5% 초과)은 night_orders가 아니라 **분석/시나리오 섹션**에 넣어라.
  · 예: 현재가 ₩1,173,000인데 ₩1,100,000 매수 → night_orders ❌, 분석에 "₩1,100,000 눌림목 시 매수 검토" ✅
  · 예: 현재가 $534인데 $490 손절 → night_orders ❌, 분석에 "GTC 손절 $490 설정 검토" ✅
  · 예: 현재가 $940인데 $950 익절 → 1.1% 차이 → night_orders ✅ (체결 가능)
- **매도 익절**: 현재가 근접(±3% 이내)에서 체결 가능한 것만 night_orders
- **매도 손절**: 현재가보다 훨씬 낮은 손절가는 분석에 "GTC 손절선 검토"로, night_orders에는 넣지 마라
- 매수 주문이 있으면 → investment_decision은 "매수실행" 또는 "소액분할"
- 매도 주문만 있으면 → investment_decision은 "매도실행" 또는 "리밸런싱"
- 매수도 매도도 없으면 → night_orders는 빈 배열 [], investment_decision은 "관망" 또는 "매수대기"
- **"대기인데 일단 넣어라"는 절대 금지**

구분 기준:
| | night_orders (즉시 실행) | 분석 섹션 (참고) |
|---|---|---|
| 현재가 ±3% 이내 익절/손절 | ✅ | |
| 현재가 ±5% 초과 목표가 | | ✅ "도달 시 실행" |
| 미래 눌림목 매수 | | ✅ "조건 충족 시 검토" |
| GTC 손절선 설정 | | ✅ "방어선 설정 검토" |

- **모든 매수 추천에 지정가 필수**: "진입가 ₩58,000 이하" 형식으로 구체적 가격 제시
- **주문 유효 시간 명시**: "내일 09:00~09:30 시초가 매수" 또는 "종일 지정가" 등
- **갭 시나리오 분석 필수**: 미국장 결과 기반으로 내일 갭업/갭다운/보합 시나리오별 대응 (분석 섹션에)
- **"내일 아침 예약 주문 요약"** 섹션을 반드시 포함 — MTS/HTS에 바로 입력 가능한 형태:
  · 구분(매수/매도) | 종목명 | 계좌 | 지정가 | 수량 | 유효시간 | 사유
- 매수도 매도도 없으면 night_orders를 빈 배열 []로.
- 가격이 이미 올라서 매수 타이밍을 놓친 경우 → 분석에 "다음 눌림목 대기" 판단, night_orders는 빈 배열"""
    elif briefing_type == "KR_BEFORE":
        market_focus = """
⑥ 이 브리핑은 【한국 시장 중심】입니다. 한국 종목(삼성전자, 한화에어로스페이스, 국내 ETF 등)에 초점을 맞추세요.
⑦ 코스피/코스닥 동향, 외국인/기관 수급, 원달러 환율이 핵심입니다.
⑧ 미국 시장은 한국장에 미치는 영향 관점에서만 간략히 언급하세요.

━━━ 💵 한국장 매매 주문 규칙 ━━━

⚠️ **strategy_buy / strategy_sell = 브리핑 받자마자 HTS/MTS에 바로 입력할 주문만**:
- 매수/매도 추천은 "분석"이 아니라 **"지금 당장 실행할 주문"**이다.
- 현재가 기준으로 **오늘 장중 체결 가능한 가격**만 진입가/목표가로 제시하라.
- 현재가와 동떨어진 가격(현재가 대비 ±5% 초과)은 매수/매도 추천이 아니라 **분석/시나리오 섹션**에 넣어라.
  · 예: 삼성전자 현재가 ₩317,000인데 ₩290,000 매수 → 추천 ❌, 분석에 "₩290,000 눌림목 시 매수 검토" ✅
  · 예: 한화에어로 현재가 ₩1,173,000인데 ₩1,100,000 매수 → 추천 ❌, 분석에 "조정 시 검토" ✅
  · 예: 삼성전자 현재가 ₩317,000, 익절 ₩325,000 → 2.5% 차이 → 추천 ✅
- **매도 익절**: 현재가 근접(±3% 이내)에서 체결 가능한 것만 추천
- **매도 손절**: 현재가보다 훨씬 낮은 손절가는 분석에 "GTC 손절선 검토"로

구분 기준:
| | 매수/매도 추천 (즉시 실행) | 분석 섹션 (참고) |
|---|---|---|
| 현재가 ±3% 이내 진입/익절 | ✅ | |
| 현재가 ±5% 초과 목표가 | | ✅ "도달 시 실행" |
| 미래 눌림목 매수 | | ✅ "조건 충족 시 검토" |
| GTC 손절선 설정 | | ✅ "방어선 설정 검토" |"""
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
        limit_order_rules = """

━━━ 💵 지정가 주문 규칙 ━━━

⚠️ **night_orders = 브리핑 받자마자 HTS에 바로 입력할 주문 (가장 중요)**:
- night_orders는 "분석"이 아니라 **"지금 당장 실행할 주문"**이다.
- 현재가 기준으로 **오늘 밤~내일 체결 가능한 가격**만 지정가로 넣어라.
- 현재가와 동떨어진 가격(현재가 대비 ±5% 초과)은 night_orders가 아니라 **분석/시나리오 섹션**에 넣어라.
  · 예: 현재가 $534인데 $490 손절 → night_orders ❌, 분석에 "GTC 손절 $490 설정 검토" ✅
  · 예: 현재가 $940인데 $950 익절 → 1.1% 차이 → night_orders ✅ (체결 가능)
- **매도 익절**: 현재가 근접(±3% 이내)에서 체결 가능한 것만 night_orders
- **매도 손절**: 현재가보다 훨씬 낮은 손절가는 분석에 "GTC 손절선 검토"로
- investment_decision이 "관망" 또는 "매수대기"이면 → night_orders는 **빈 배열 []**
- night_orders에 주문이 있으면 → investment_decision은 "매수실행" 또는 "소액분할"
- **"대기인데 주문은 넣어라"는 절대 금지**

- **모든 매수 추천에 지정가 필수**: "진입가 $185.00 이하" 형식
- **주문 유효 기간 명시**: "당일 지정가(DAY)" 또는 "GTC(취소 전 유효)" 등
- **"오늘 밤 지정가 주문 요약"** 섹션을 반드시 포함 — HTS에 바로 입력 가능한 형태:
  · 구분(매수/매도) | 종목명 | 계좌 | 지정가 | 수량 | 유효기간 | 사유
- 매수도 매도도 없으면 night_orders를 빈 배열 []로.
- 가격이 이미 올라서 매수 타이밍을 놓친 경우 → 분석에 "다음 눌림목 대기", night_orders는 빈 배열"""
        market_focus = f"""
⑥ 이 브리핑은 【미국 시장 중심】입니다. 미국 종목({us_focus_tickers})에 초점을 맞추세요.
⑦ S&P500/나스닥/다우 동향, Fed 정책, VIX, 미국 국채 금리가 핵심입니다.
⑧ 한국 시장은 미국장에 미치는 영향 관점에서만 간략히 언급하세요.
{ria_focus}{limit_order_rules}"""
    elif briefing_type == "US_NIGHT":
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
⑥ 이 브리핑은 【미국 시장 야간 프리브리핑】입니다. **오늘 밤 미국장 지정가 예약 주문**을 위한 사전 분석입니다.
⑦ S&P500/나스닥/다우 동향, Fed 정책, VIX, 미국 국채 금리, 프리마켓 선물이 핵심입니다.
⑧ 한국 시장은 미국장에 미치는 영향 관점에서만 간략히 언급하세요.
{ria_focus}

━━━ 🌙 미국장 프리브리핑 특화 규칙 ━━━

⚠️ **night_orders = 브리핑 받자마자 HTS에 바로 입력할 주문 (가장 중요)**:
- night_orders는 "분석"이 아니라 **"지금 당장 실행할 주문"**이다.
- 현재가 기준으로 **오늘 밤 체결 가능한 가격**만 지정가로 넣어라.
- 현재가와 동떨어진 가격(현재가 대비 ±5% 초과)은 night_orders가 아니라 **분석/시나리오 섹션**에 넣어라.
  · 예: MU 현재가 $940, 익절 $950 → 1.1% 차이 → night_orders ✅
  · 예: LMT 현재가 $534, 손절 $490 → 8.2% 차이 → night_orders ❌, 분석에 "GTC 손절 $490 설정 검토" ✅
- **매도 익절**: 현재가 근접(±3% 이내)에서 체결 가능한 것만 night_orders
- **매도 손절**: 현재가보다 훨씬 낮은 손절가는 분석에 "GTC 손절선 검토"로
- investment_decision이 "관망" 또는 "매수대기"이면 → night_orders는 **빈 배열 []**
- night_orders에 주문이 있으면 → investment_decision은 "매수실행" 또는 "소액분할"
- **"대기인데 주문은 넣어라"는 절대 금지**

구분 기준:
| | night_orders (즉시 실행) | 분석 섹션 (참고) |
|---|---|---|
| 현재가 ±3% 이내 익절/손절 | ✅ | |
| 현재가 ±5% 초과 목표가 | | ✅ "도달 시 실행" |
| 미래 눌림목 매수 | | ✅ "조건 충족 시 검토" |
| GTC 손절선 설정 | | ✅ "방어선 설정 검토" |

- **모든 매수 추천에 지정가 필수**: "진입가 $185.00 이하" 형식
- **주문 유효 기간 명시**: "당일 지정가(DAY)" 또는 "GTC(취소 전 유효)" 등
- **프리마켓/선물 분석 필수**: 선물 동향 기반 시나리오별 대응 (분석 섹션에)
- **"오늘 밤 지정가 주문 요약"** 섹션 포함 — HTS에 바로 입력 가능한 형태:
  · 구분(매수/매도) | 종목명 | 계좌 | 지정가 | 수량 | 유효기간 | 사유
- 매수도 매도도 없으면 night_orders를 빈 배열 []로.
- 개장 전 2.5시간 여유가 있으므로 → **주문 넣고 자도 되는지** 명시"""
    elif briefing_type == "US_CLOSE":
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
⑥ 이 브리핑은 【미국장 마감 요약】입니다. 밤 동안 미국장에서 벌어진 일을 한국 아침에 전달합니다.
⑦ 보유 미국 종목({us_focus_tickers})의 종가·등락·거래량 변화가 핵심입니다.
⑧ 오늘 한국장에 미칠 영향 + 보유 포지션 조치 필요 여부를 간결하게 요약하세요.
{ria_focus}

━━━ 🌅 미국장 마감 요약 규칙 ━━━
- 보유 종목 종가 + 등락률 + 밤새 주요 뉴스 핵심만
- 보유 포지션에 즉각 행동 필요하면 night_orders에 포함 (한국장 시초가 ETF 매매 등)
- 행동 불필요하면 night_orders 빈 배열 + 이유 한 줄
- 간결하게 — 상세 분석은 저녁 US_NIGHT에서"""
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

    # 보유 종목 손익 계산 (current_prices 캐시 활용 — API 과호출 방지)
    _prices = current_prices or {}
    _holding_alerts: list[str] = []

    def _fmt_holding(tk: str, info: dict) -> str:
        shares = info.get("shares", 0)
        ria = info.get("ria_eligible", 0)
        ria_tag = f" [RIA 적격 {ria}주]" if ria > 0 else ""
        cur_price = _prices.get(tk, 0)
        if "avg_cost_usd" in info:
            avg = info['avg_cost_usd']
            pnl = (cur_price - avg) / avg * 100 if avg and cur_price else 0
            pnl_str = f" → 현재 ${cur_price:,.2f} ({pnl:+.1f}%)" if cur_price else ""
            if pnl <= -10:
                _holding_alerts.append(f"🚨 {tk}: 매수 ${avg:.2f} → 현재 ${cur_price:.2f} ({pnl:+.1f}%) — 손절 검토 필요")
            elif pnl <= -5:
                _holding_alerts.append(f"⚠️ {tk}: 매수 ${avg:.2f} → 현재 ${cur_price:.2f} ({pnl:+.1f}%) — 주의")
            return f"  {tk}: {shares}주 (매수 ${avg:.2f}{pnl_str}){ria_tag}"
        else:
            avg = info.get('avg_cost_krw', 0)
            pnl = (cur_price - avg) / avg * 100 if avg and cur_price else 0
            pnl_str = f" → 현재 ₩{cur_price:,.0f} ({pnl:+.1f}%)" if cur_price else ""
            if pnl <= -10:
                _holding_alerts.append(f"🚨 {tk}: 매수 ₩{avg:,.0f} → 현재 ₩{cur_price:,.0f} ({pnl:+.1f}%) — 손절 검토 필요")
            elif pnl <= -5:
                _holding_alerts.append(f"⚠️ {tk}: 매수 ₩{avg:,.0f} → 현재 ₩{cur_price:,.0f} ({pnl:+.1f}%) — 주의")
            return f"  {tk}: {shares}주 (매수 ₩{avg:,.0f}{pnl_str})"

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

━━━ 매매 프레임워크 (모든 판단의 기준) ━━━

【A. 판단 원칙】
- 데이터 기반 냉정한 판단. 아부 금지, 직언만.
- 너무 보수적(기회 놓침)도, 너무 공격적(도박)도 안 됨 — 중립적 포지션.
- 모든 수치는 구체적으로 (%, 가격, 수량, 계좌).
- 시세 미수집 종목은 가격 추측 금지 — "시세 확인 필요"로만 출력.
- AI 메모리 신뢰도 보정 반영: 🔴 위험 종목 -15~-30% 감점, 🟢 고신뢰 +5~+10% 가중.

【B. 매수 판단 — "왜 지금 사야 하는가?"가 명확할 때만】
- 매수 추천 시 반드시 포함: 계좌, 종목, 수량, 지정가, 손절가, 목표가, 근거.
- 매수 조건 최소 2개 이상 충족 시에만 추천:
  · RSI 35 이하 과매도 + OBV 매수 또는 중립 (OBV 매도면 매수 금지)
  · MACD 매수 전환 + 거래량 증가
  · 핵심 지지선 확인 + 반등 캔들
  · 이벤트 카탈리스트 (실적, 정책, 제품 출시 등) + 기술적 저점
- 조건 1개만 충족: 관망. "RSI 낮다"만으로 매수 추천 금지.
- 분할 매수 원칙: 1회 매수 금액은 해당 계좌 예수금의 30% 이내.
- Watchlist 신규 종목도 동일 기준 적용 — 진입가/손절/익절 구체화.
- **매 브리핑마다 strategy_buy에 신규 진입 후보 최소 1개를 검토**할 것 (Watchlist/RIA ETF에서).
  · 조건 미충족이면 추천하지 않아도 되지만, "신규 후보 중 왜 매수 보류인지" 이유를 strategy_summary에 명시.
  · "검토할 신규 후보 없음"은 금지 — Watchlist에 종목이 있으므로 최소 1개는 언급.
- 매수 추천은 구분 표시:
  · "추가매수" — 이미 보유 종목 추가 매수
  · "신규진입" — 비보유 Watchlist/RIA ETF에서 새로 진입

【C. 매도 판단 — 매수만큼 중요. 매 브리핑마다 전 보유종목 점검 필수】
- 아래 보유 종목 손익 경고(🚨/⚠️)를 반드시 읽고 strategy_sell에 반영할 것.
- 손절 기준:
  · 매수가 대비 -10% 이상 → 🚨 명확한 홀딩 근거 없으면 즉시 매도 추천
  · 매수가 대비 -7% 이상 + OBV 매도 + MACD 매도 → 🚨 "추세 훼손" 매도 추천
  · 손절가 이탈 → 무조건 매도 추천 (예외 없음)
- 익절 기준:
  · 목표가 도달 → 최소 50% 물량 익절 추천 (전량 보유는 명확한 추가 상승 근거 필요)
  · RSI 75+ 과매수 + OBV 둔화 → 부분 익절 검토
- 홀딩 허용 조건 (손실 중이라도):
  · 명확한 카탈리스트 대기 (실적, 이벤트) + 기한 명시
  · OBV 매수 유지 + 지지선 미이탈
  · 반드시 "언제까지 홀딩, 무엇이 무효화 조건인지" 명시
- "관망"이면서 손실 종목을 언급 안 하는 것은 **가장 나쁜 판단**.
- 서킷브레이커 발동 중이라도 **매도 추천은 억제하지 마라**.

【D. 커뮤니케이션 — 사용자가 즉시 행동할 수 있도록】
- 결론 먼저, 근거 나중. 표 적극 활용.
- 모든 매수/매도에 반드시: [계좌] + 종목명 + 수량 + 지정가.
- 모호한 표현 금지: "검토 필요" → "₩X 이하 시 매도", "기회 있을 수 있음" → 구체적 트리거.
- 보유 종목 중 문제 있는 것부터 먼저 말하고, 그 다음 매수 기회.
- 분석가 간 의견 충돌이 있으면 양쪽 논리를 명시하고 CIO 판단 제시.
- 관망 판단이면: "왜 지금은 안 되는지" + "어떤 조건이면 행동할지" 트리거 필수.

【E. 리스크 관리】
- 단일 종목 비중: 계좌당 최대 30% (초과 시 경고).
- 전체 포트폴리오: 위험 종목(낙폭 -10%+) 3개 이상이면 신규 매수 중단.
- 실적 D-3 이내 종목: 신규 매수 금지, 보유 시 이벤트 리스크 명시.
- 상관관계 높은 종목(0.8+) 동시 매수 금지.

━━━ 실제 보유 포지션 (현재가 + 손익률 포함) ━━━
{holdings_text}
예수금: ₩{DEFAULT_CASH:,.0f}

━━━ 🚨 보유 종목 손익 경고 (C항 매도 판단 기준 적용 필수) ━━━
{chr(10).join(_holding_alerts) if _holding_alerts else "✅ 모든 보유 종목 정상 범위"}

━━━ 계좌 규칙 ━━━
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

    # 야간/프리브리핑: 예약 주문 요약 JSON 스키마 추가 (매수 + 매도 모두 포함)
    if briefing_type == "KR_NIGHT":
        night_orders_schema = """,
  "night_orders": [
    {
      "구분": "매수|매도",
      "종목": "종목명",
      "계좌": "[ISA]|[일반]|[RIA]",
      "지정가": "매수: ₩58,000 이하 / 매도: ₩65,000 이상",
      "수량": "10주",
      "유효시간": "09:00~15:30 또는 시초가",
      "조건": "갭다운 시 ₩56,000으로 변경 / 익절·손절 조건",
      "사유": "매수 근거 또는 익절·손절·리밸런싱 사유"
    }
  ],
  "gap_scenarios": {
    "갭업_1pct_이상": "매수: 추격 여부. 매도: 익절 지정가 상향 여부",
    "보합": "기본 지정가 유지 여부",
    "갭다운_1pct_이상": "매수: 하향 지정가. 매도: 손절 지정가 하향 여부"
  }"""
    elif briefing_type in ("US_NIGHT", "US_BEFORE", "US_CLOSE"):
        night_orders_schema = """,
  "night_orders": [
    {
      "구분": "매수|매도",
      "종목": "종목명",
      "계좌": "[일반]",
      "지정가": "매수: $185.00 이하 / 매도: $200.00 이상",
      "수량": "5주",
      "유효기간": "DAY 또는 GTC",
      "조건": "프리마켓 약세 시 $180.00으로 변경 / 익절·손절 조건",
      "사유": "매수 근거 또는 익절·손절 사유"
    }
  ],
  "gap_scenarios": {
    "선물_상승": "매수: 추격 여부. 매도: 익절 지정가 상향 여부",
    "선물_보합": "기본 지정가 유지 여부",
    "선물_하락": "매수: 하향 지정가. 매도: 손절 지정가 하향 여부"
  }"""
    else:
        night_orders_schema = ""

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
      "reason": "매수 근거 상세",
      "strategy_type": "단기매매|중기보유|리밸런싱|세금전략|일반",
      "strategy_tags": ["RSI반등", "볼린저하단", "펀더멘털성장 등 해당하는 전략 태그"],
      "horizon_days": 7,
      "benchmark_ticker": "^KS11|^IXIC|^GSPC",
      "execution_condition": "지금|지정가 조건|장마감 확인 후",
      "invalidation_condition": "손절가 이탈|뉴스 반전|기술 신호 훼손",
      "risk_reward": 2.0
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
      "reason": "매도 근거",
      "strategy_type": "단기매매|중기보유|리밸런싱|세금전략|일반",
      "strategy_tags": ["과열매도", "이벤트드리븐", "세금전략 등 해당하는 전략 태그"],
      "horizon_days": 7,
      "benchmark_ticker": "^KS11|^IXIC|^GSPC",
      "execution_condition": "즉시|RSI 70+ 시|장마감 확인 후",
      "invalidation_condition": "목표가 도달|추세 반전|세금 이벤트 종료",
      "risk_reward": 1.5
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
  }}{night_orders_schema}
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
