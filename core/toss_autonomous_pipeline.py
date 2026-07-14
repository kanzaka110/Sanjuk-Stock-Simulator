"""core/toss_autonomous_pipeline.py

품질게이트 PASS_EXECUTE 후보 → 자동 preview/검증/판정 파이프라인.

[배경]
기존 흐름은 preview 생성과 Hermes 판정이 전부 수동이라 후보가 previewed
상태로 정체됐다 (7일간 231건 preview 중 실주문 12건). 이 모듈은 그 병목을
제거한다: 품질게이트를 통과한(stock_agent_ready) 후보를 자동으로
  preview → ledger 기록 → 검증 요청 생성 → 자동 판정(PASS/HOLD/BLOCK)
까지 연결한다. 판정이 PASS면 record_hermes_verification 내부에서
autonomous finalizer가 자동 발동한다 (기존 경로 재사용).

Hermes는 차단자가 아니라 사후 감사자 — 판정 결과와 사유는 전부
verification DB에 남으므로 사후 검증 가능.

[안전장치 — 전부 기존 경로 유지]
- TOSS_AUTONOMOUS_MODE=false → no-op (finalizer도 이중 차단)
- TOSS_AUTONOMOUS_KILL_SWITCH=true → no-op
- TOSS_AUTO_PIPELINE_ENABLED=false → 이 파이프라인만 개별 비활성화
- KR/US 각 거래 가능 세션에만 동작
- 자동 판정은 build_default_hermes_verdict 규칙 사용 (sell guard/
  blocked symbol/금액/가격 검증)
- 실행 직전 can_send_live_pilot_order 가드 체인 + cross_check는
  finalizer가 그대로 수행 (중복 주문 방지 포함)
- 심볼당 1일 1회만 파이프라인 시도 (상태 파일 dedup)

[진단]
거래가 없으면 왜 없었는지 no_action_diagnosis를 상태 파일에 남긴다
(일일 리포트가 소비).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_DEFAULT_THROTTLE_MINUTES = 10  # 파이프라인 실행 최소 간격 (기본 10분, env로 조정)
_MIN_THROTTLE_MINUTES = 5       # 과도 단축 방지 하한
_MAX_ATTEMPTS_PER_RUN = 3       # 1회 실행당 최대 후보 처리 수
_STATE_FILE = "toss_auto_pipeline_state.json"


def _throttle_minutes() -> int:
    """장중 스캔 주기 (env TOSS_PIPELINE_INTERVAL_MIN, 기본 10분, 최소 5분)."""
    raw = os.environ.get("TOSS_PIPELINE_INTERVAL_MIN", "").strip()
    try:
        val = int(raw) if raw else _DEFAULT_THROTTLE_MINUTES
    except ValueError:
        val = _DEFAULT_THROTTLE_MINUTES
    return max(_MIN_THROTTLE_MINUTES, val)


# ── env / 상태 ───────────────────────────────────────────────────

def _pipeline_enabled() -> bool:
    """파이프라인 개별 스위치 (기본 ON — autonomous mode가 마스터 게이트)."""
    raw = os.environ.get("TOSS_AUTO_PIPELINE_ENABLED", "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _active_execution_market(now: datetime | None = None) -> str:
    """현재 자동 실행 가능한 시장 반환: KR / US / ALL / ''.

    KR은 한국 정규장, US는 프리+정규+애프터를 주문 가능 세션으로 본다.
    기존 KR-only 게이트 때문에 US 후보가 밤에 실행되지 않던 문제를 분리한다.
    """
    from core.market_hours import get_market_session, is_kr_market_open

    kr_open = is_kr_market_open(now)
    session = get_market_session(now)
    us_tradeable = session.get("us") in {"US_PREMARKET", "US_REGULAR", "US_AFTERMARKET"}
    if kr_open and us_tradeable:
        return "ALL"
    if us_tradeable:
        return "US"
    if kr_open:
        return "KR"
    return ""


def _state_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "db" / "data" / _STATE_FILE


def _load_state() -> dict:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("auto pipeline state load failed: %s", e)
    return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("auto pipeline state save failed: %s", e)


# ── 후보 선별 ────────────────────────────────────────────────────

def select_ready_candidates(limit: int = 10, market: str = "KR") -> tuple[list[dict], list[dict]]:
    """stock_agent_ready 후보와 미달 후보(사유 포함)를 분리 반환.

    market: "KR" | "US" | "ALL". 자동 파이프라인은 현재 거래 가능 세션의
    시장만 후보로 가져와 KR 원화 소진이 US 달러 후보를 밀어내지 않게 한다.

    Returns:
        (ready, not_ready) — not_ready 항목은 진단용 {symbol, reason}
    """
    from core.dashboard_data import toss_buy_candidates_data

    data = toss_buy_candidates_data(limit=limit, market=market) or {}
    items = data.get("items") or []
    ready: list[dict] = []
    not_ready: list[dict] = []
    for item in items:
        income = item.get("income_strategy") or {}
        side = str(item.get("side") or "buy").lower()
        # exact bool만 신뢰 — 문자열 "false"/"true"·정수 1은 직렬화 오염 신호 (fail-closed)
        income_ok = side != "buy" or income.get("income_pass") is True
        if item.get("stock_agent_ready") is True and income_ok:
            ready.append(item)
        else:
            reason = str(
                income.get("income_block_label")
                or income.get("income_block_reason")
                or ("income_strategy_missing" if side == "buy" and not income else "")
                or item.get("block_reason")
                or item.get("decision_reason")
                or item.get("execution_status")
                or ("missing: " + ",".join(item.get("missing_fields") or []) if item.get("missing_fields") else "")
                or "unknown"
            )
            not_ready.append({
                "symbol": item.get("symbol") or item.get("ticker") or "",
                "reason": reason,
            })

    def _ready_sort_key(item: dict) -> tuple[float, float, float, float]:
        income = item.get("income_strategy") or {}
        def _f(v) -> float:
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0
        return (
            _f(income.get("expected_pnl_krw")),
            _f(income.get("income_edge_ratio")),
            _f(item.get("risk_reward")),
            _f(item.get("score")),
        )

    ready.sort(key=_ready_sort_key, reverse=True)
    return ready, not_ready


# ── 후보 1건 처리 ────────────────────────────────────────────────

def _autonomous_sides(policy: dict) -> list[str]:
    """자율실행 허용 side 목록 (env TOSS_AUTONOMOUS_ALLOWED_SIDES 기반)."""
    sides = policy.get("autonomous_allowed_sides")
    if not sides:
        return ["buy"]
    return [str(s).lower() for s in sides]


def process_candidate(
    candidate: dict,
    policy: dict,
    reason: str = "auto_pipeline",
    note: str = "",
) -> dict:
    """후보 1건: preview → ledger → 검증 요청 → 자동 판정 기록.

    판정 PASS면 record_hermes_verification 내부에서 finalizer 자동 발동.

    Args:
        reason: ledger/검증 기록에 남길 경로 식별자 (auto_pipeline / auto_exit_sell 등)
        note: 판정 기록에 덧붙일 설명 (기본: 품질게이트 요약)

    Returns:
        {"symbol", "stage", "verdict"?, "pilot_id"?, "reason"?}
    """
    from core.toss_live_pilot_preview import build_live_pilot_preview
    from core.toss_live_pilot_ledger import record_live_pilot_preview
    from core.toss_live_pilot_verification import (
        create_verification_request,
        record_hermes_verification,
        build_hermes_verification_context,
    )
    from core.toss_live_pilot_hermes_bridge import build_default_hermes_verdict

    symbol = str(candidate.get("symbol") or candidate.get("ticker") or "")
    side = str(candidate.get("side") or "buy").lower()
    income = candidate.get("income_strategy") or {}
    if side == "buy" and not income.get("income_pass"):
        return {
            "symbol": symbol,
            "stage": "income_gate_blocked",
            "reason": str(
                income.get("income_block_label")
                or income.get("income_block_reason")
                or "income_strategy_missing"
            ),
        }

    # AI Berkshire BUY 게이트 — dashboard 후보 정규화와 독립 재검사.
    # stale preview / API 우회로 avoid 또는 checklist fail/gray_zone 종목이
    # 들어와도 preview/finalizer/transport에 도달하지 못하게 한다.
    # BUY에만 적용하며 SELL/손절/익절 경로는 불변이다.
    if side == "buy":
        from core.ai_berkshire_toss import evaluate_ai_berkshire_buy_gate
        try:
            gate = evaluate_ai_berkshire_buy_gate(symbol)
        except Exception as e:
            log.warning("ai_berkshire buy gate recheck failed (%s): %s", symbol, e)
            return {
                "symbol": symbol,
                "stage": "ai_berkshire_buy_blocked",
                "reason": "ai_berkshire_gate_error",
            }
        gate_reason = str(gate.get("buy_reason") or "ai_berkshire_buy_blocked")
        if gate.get("buy_block") or gate_reason == "ai_berkshire_scores_unavailable":
            stage = (
                "ai_berkshire_avoid_blocked"
                if gate_reason == "ai_berkshire_avoid"
                else "ai_berkshire_buy_blocked"
            )
            log.info("auto pipeline: %s blocked by ai_berkshire (%s)", symbol, gate_reason)
            return {
                "symbol": symbol,
                "stage": stage,
                "reason": gate_reason,
            }

    preview_input = {
        "symbol": symbol,
        "side": str(candidate.get("side") or "buy").lower(),
        "quantity": int(candidate.get("quantity") or 0),
        "limit_price": float(candidate.get("limit_price") or candidate.get("entry_price") or 0),
        "stop_loss": candidate.get("stop_loss"),
        "invalidation": candidate.get("invalidation"),
        "target_price": candidate.get("target_price"),
        "decision_ref": candidate.get("decision_ref"),
        "source_prediction_id": candidate.get("source_prediction_id"),
    }

    # 1. preview
    preview = build_live_pilot_preview(preview_input, policy=policy)
    if not preview.get("ok"):
        return {
            "symbol": symbol,
            "stage": "preview_blocked",
            "reason": preview.get("block_summary", "preview blocked"),
        }

    # 2. ledger
    rec = record_live_pilot_preview(preview, reason=reason)
    pilot_id = rec.get("pilot_id", "")
    if not pilot_id:
        return {"symbol": symbol, "stage": "ledger_failed", "reason": "no pilot_id"}

    # 실행 판단 outcome 계약: 모든 BUY는 executable bucket + exact pilot/ref 품질 row가
    # 기록돼야만 검증·finalizer로 진행한다. SELL 리스크 경로는 별도 P&L 계약이다.
    if side == "buy":
        from core.toss_quality_gate import (
            EXECUTABLE_BUCKETS,
            record_execution_quality_decision,
        )
        bucket = str(candidate.get("decision_bucket") or "")
        if bucket not in EXECUTABLE_BUCKETS:
            return {
                "symbol": symbol,
                "stage": "quality_attribution_failed",
                "pilot_id": pilot_id,
                "reason": "non_executable_decision_bucket",
            }
        try:
            quality_record = record_execution_quality_decision(
                candidate,
                pilot_id=pilot_id,
                decision_ref=str(preview.get("decision_ref") or ""),
            )
        except Exception as exc:
            log.error("quality attribution failed: %s", type(exc).__name__)
            return {
                "symbol": symbol,
                "stage": "quality_attribution_failed",
                "pilot_id": pilot_id,
                "reason": "quality_record_exception",
            }
        if not quality_record.get("ok"):
            return {
                "symbol": symbol,
                "stage": "quality_attribution_failed",
                "pilot_id": pilot_id,
                "reason": str(quality_record.get("reason") or "quality_record_failed"),
            }

    # 3. 검증 요청 (PENDING)
    verif_preview = {**preview, "pilot_id": pilot_id}
    verif = create_verification_request(verif_preview, pilot_id=pilot_id)
    verification_id = verif.get("verification_id", "")
    if not verification_id:
        return {
            "symbol": symbol, "stage": "verification_request_failed",
            "pilot_id": pilot_id, "reason": "no verification_id",
        }

    # 4. 자동 판정 — 검증 컨텍스트에 정책 한도/허용 방향 명시
    ctx = build_hermes_verification_context(verif_preview, policy)
    ctx["verification_id"] = verification_id
    ctx["max_order_krw"] = policy.get("max_order_krw") or 0  # 0 = 무제한
    ctx["allowed_sides"] = _autonomous_sides(policy)
    verdict = build_default_hermes_verdict(ctx)
    status = verdict.get("status", "HOLD")

    quality_note = note or _quality_note(candidate)
    result = record_hermes_verification(
        verification_id=verification_id,
        status=status,
        reasons=list(verdict.get("reasons") or []) + [quality_note],
        checks=verdict.get("checks") or {},
        hermes_message=f"auto_verifier({reason}): {quality_note}",
    )

    return {
        "symbol": symbol,
        "stage": "verdict_recorded" if result.get("ok") else "verdict_record_failed",
        "verdict": status,
        "pilot_id": pilot_id,
        "verification_id": verification_id,
        "decision_ref": preview.get("decision_ref", ""),
        "reason": "; ".join(verdict.get("reasons") or []),
    }


def _quality_note(candidate: dict) -> str:
    bucket = candidate.get("decision_bucket", "")
    score = candidate.get("score")
    rr = candidate.get("risk_reward")
    parts = []
    if bucket:
        parts.append(f"bucket={bucket}")
    if score is not None:
        parts.append(f"score={score}")
    if rr is not None:
        parts.append(f"rr={rr}")
    income = candidate.get("income_strategy") or {}
    if income:
        parts.append(f"expected_pnl={income.get('expected_pnl_krw')}")
        parts.append(f"income_grade={income.get('income_grade')}")
    return " ".join(parts) or "quality_gate_pass"


# ── retryable 주문 재시도 스윕 ───────────────────────────────────

_MAX_RETRIES = 3


def retry_retryable_orders(now: datetime | None = None, state: dict | None = None) -> dict:
    """당일 live_send_retryable 주문 재시도.

    각 pilot_id에 대해:
      1. 새 검증 요청 생성 + 자동 판정 (기존 PASS는 TTL 만료됐을 것)
      2. PASS면 try_autonomous_finalize(allow_retry=True) 직접 호출
      3. 재시도 횟수(state 기록) 초과 시 terminal failed로 전환

    Returns:
        {"retried": int, "sent": int, "exhausted": int}
    """
    now = now or datetime.now(KST)
    own_state = state is None
    if own_state:
        state = _load_state()

    today = now.strftime("%Y-%m-%d")
    retry_counts = state.get("retry_counts", {})
    if state.get("retry_date") != today:
        retry_counts = {}

    from core.toss_live_pilot_ledger import (
        list_live_pilot_records,
        record_live_send_failed,
    )
    from core.toss_live_pilot_verification import (
        create_verification_request,
        record_hermes_verification,
        build_hermes_verification_context,
    )
    from core.toss_live_pilot_hermes_bridge import build_default_hermes_verdict
    from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
    from core.toss_autonomous_finalizer import try_autonomous_finalize

    policy = compute_toss_live_pilot_policy()
    retried = sent = exhausted = 0

    try:
        records = list_live_pilot_records(limit=100)
    except Exception as e:
        log.warning("retry sweep ledger fetch failed: %s", e)
        records = []

    for rec in records:
        if rec.get("status") != "live_send_retryable":
            continue
        if not str(rec.get("created_at", "")).startswith(today):
            continue
        pilot_id = rec.get("pilot_id", "")
        if not pilot_id:
            continue

        count = int(retry_counts.get(pilot_id, 0))
        if count >= _MAX_RETRIES:
            try:
                record_live_send_failed(
                    pilot_id,
                    failure_reason=f"retry_exhausted({count}): {rec.get('failure_reason', '')}"[:500],
                )
                exhausted += 1
            except Exception as e:
                log.warning("retry exhausted record failed: %s", e)
            continue

        retry_counts[pilot_id] = count + 1
        retried += 1
        try:
            preview_stub = {
                "symbol": rec.get("symbol", ""),
                "side": rec.get("side", "buy"),
                "quantity": rec.get("quantity", 0),
                "limit_price": rec.get("limit_price", 0),
                "estimated_amount_krw": rec.get("estimated_amount_krw", 0),
                "decision_ref": rec.get("decision_ref", ""),
                "preview_id": rec.get("preview_id") or pilot_id,
                "pilot_id": pilot_id,
            }
            verif = create_verification_request(preview_stub, pilot_id=pilot_id)
            verification_id = verif.get("verification_id", "")
            ctx = build_hermes_verification_context(preview_stub, policy)
            ctx["verification_id"] = verification_id
            ctx["max_order_krw"] = policy.get("max_order_krw") or 0
            ctx["allowed_sides"] = _autonomous_sides(policy)
            verdict = build_default_hermes_verdict(ctx)
            record_hermes_verification(
                verification_id=verification_id,
                status=verdict.get("status", "HOLD"),
                reasons=list(verdict.get("reasons") or []) + [f"retry_attempt={count + 1}"],
                checks=verdict.get("checks") or {},
                hermes_message="auto_verifier(retry_sweep)",
            )
            if verdict.get("status") == "PASS":
                result = try_autonomous_finalize(pilot_id, allow_retry=True)
                if result.get("live_order_sent"):
                    sent += 1
        except Exception as e:
            log.warning("retry sweep pilot=%s failed: %s", pilot_id, e)

    state["retry_date"] = today
    state["retry_counts"] = retry_counts
    if own_state:
        _save_state(state)

    if retried or exhausted:
        log.info("retry sweep: retried=%d sent=%d exhausted=%d", retried, sent, exhausted)
    return {"retried": retried, "sent": sent, "exhausted": exhausted}


# ── 자본 가동률 KPI + 일일 리포트 ────────────────────────────────

_DEPLOYMENT_TARGET_MIN = 0.60
_DEPLOYMENT_TARGET_MAX = 0.80
_REPORT_HOUR_KST = 16  # KR 장 마감 후 리포트 발송 시각 (KST 16시 이후 첫 루프)


def compute_deployment_kpi() -> dict:
    """Toss 계좌 자본 가동률 KPI (목표 60~80%).

    가동률 = 평가금액(KRW 환산) / (평가금액 + 현금).
    계좌 조회 실패 시 fail-safe {"ok": False}.
    """
    try:
        from core.dashboard_data import toss_account_summary
        summary = toss_account_summary() or {}
    except Exception as e:
        return {"ok": False, "reason": f"account_summary_failed: {e}"[:200]}

    mv = (summary.get("market_value") or {}).get("krw")
    cash = (summary.get("cash") or {}).get("krw")
    if mv is None or cash is None:
        return {"ok": False, "reason": summary.get("error") or "market_value/cash missing"}

    try:
        mv = float(mv)
        cash = float(cash)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "non_numeric_account_values"}

    total = mv + cash
    if total <= 0:
        return {"ok": False, "reason": "total_account_value_zero"}

    rate = mv / total
    if rate < _DEPLOYMENT_TARGET_MIN:
        status = "below_target"
    elif rate > _DEPLOYMENT_TARGET_MAX:
        status = "above_target"
    else:
        status = "in_range"

    return {
        "ok": True,
        "deployment_rate": round(rate, 4),
        "market_value_krw": round(mv),
        "cash_krw": round(cash),
        "total_krw": round(total),
        "target_min": _DEPLOYMENT_TARGET_MIN,
        "target_max": _DEPLOYMENT_TARGET_MAX,
        "status": status,
    }


def _format_daily_report(kpi: dict, state: dict, today: str) -> str:
    """일일 파이프라인 리포트 텔레그램 메시지 구성."""
    lines = [f"🤖 Toss 자율매매 일일 리포트 ({today})", ""]

    # 자본 가동률
    if kpi.get("ok"):
        rate = kpi["deployment_rate"]
        status_label = {
            "below_target": "⚠️ 목표 미달 — 매수 여력 있음",
            "in_range": "✅ 목표 범위",
            "above_target": "⚠️ 목표 초과 — 현금 확보 검토",
        }.get(kpi["status"], "")
        lines.append(f"자본 가동률: {rate * 100:.1f}% (목표 60~80%) {status_label}")
        lines.append(
            f"  평가 {kpi['market_value_krw']:,.0f}원 / 현금 {kpi['cash_krw']:,.0f}원"
            f" / 총 {kpi['total_krw']:,.0f}원"
        )
    else:
        lines.append(f"자본 가동률: 조회 실패 ({kpi.get('reason', '')})")
    lines.append("")

    # 당일 파이프라인 실행 결과
    attempted_map = state.get("attempted", {}) if state.get("attempted_date") == today else {}
    pass_syms = [s for s, v in attempted_map.items() if v.get("verdict") == "PASS"]
    lines.append(f"파이프라인 시도: {len(attempted_map)}건 / PASS {len(pass_syms)}건")
    for sym, v in list(attempted_map.items())[:10]:
        lines.append(f"  · {sym}: {v.get('verdict') or v.get('stage', '')} ({v.get('at', '')})")

    # 재시도 스윕
    retry_counts = state.get("retry_counts", {}) if state.get("retry_date") == today else {}
    if retry_counts:
        lines.append(f"재시도: {len(retry_counts)}건 (횟수 {sum(retry_counts.values())})")

    # 미거래 진단
    diagnosis = state.get("no_action_diagnosis")
    if not attempted_map and diagnosis:
        lines.append("")
        lines.append(f"미거래 사유: {diagnosis.get('reason', '')}")
        for nr in (diagnosis.get("not_ready") or [])[:5]:
            lines.append(f"  · {nr.get('symbol', '?')}: {nr.get('reason', '')[:80]}")

    return "\n".join(lines)


def send_daily_pipeline_report(now: datetime | None = None, force: bool = False) -> dict:
    """장 마감 후 1일 1회 자율매매 리포트 발송 (monitor 루프에서 호출).

    - KST 16시 이후 첫 호출에 발송 (주말 제외)
    - state 파일 report_date로 dedup
    """
    now = now or datetime.now(KST)

    if not _pipeline_enabled():
        return {"skipped": "pipeline_disabled"}
    if not force:
        if now.weekday() >= 5:
            return {"skipped": "weekend"}
        if now.hour < _REPORT_HOUR_KST:
            return {"skipped": "before_report_hour"}

    state = _load_state()
    today = now.strftime("%Y-%m-%d")
    if not force and state.get("report_date") == today:
        return {"skipped": "already_sent_today"}

    kpi = compute_deployment_kpi()
    try:
        from core.toss_quality_gate import evaluate_outcomes
        outcomes = evaluate_outcomes()
    except Exception as exc:
        log.warning("quality outcome evaluation failed: %s", type(exc).__name__)
        outcomes = {
            "evaluated": 0,
            "errors": 1,
            "error": f"evaluation_failed:{type(exc).__name__}",
        }
    message = _format_daily_report(kpi, state, today)
    message += (
        f"\n\n5일 outcome 평가: {int(outcomes.get('evaluated') or 0)}건"
        f" / 오류 {int(outcomes.get('errors') or 0)}건"
    )

    try:
        from core.telegram import send_simple_message
        sent = send_simple_message(message)
    except Exception as e:
        log.warning("daily pipeline report send failed: %s", e)
        return {"sent": False, "reason": str(e)[:200], "outcomes": outcomes}

    if sent:
        state["report_date"] = today
        _save_state(state)
        log.info("daily pipeline report sent (%s)", today)
    return {"sent": bool(sent), "kpi": kpi, "outcomes": outcomes}


# ── 메인 실행 ────────────────────────────────────────────────────

def run_toss_autonomous_pipeline(
    now: datetime | None = None,
    force: bool = False,
) -> dict:
    """자동 파이프라인 1회 실행 (monitor 루프에서 호출).

    - 스로틀(기본 10분, env TOSS_PIPELINE_INTERVAL_MIN) + KR/US 거래 가능 세션에만
    - autonomous mode ON + kill switch OFF일 때만
    - 심볼당 1일 1회 시도
    - 실행 결과와 no_action_diagnosis를 상태 파일에 기록
    """
    now = now or datetime.now(KST)

    if not _pipeline_enabled():
        return {"skipped": "pipeline_disabled"}

    active_market = _active_execution_market(now)
    if force and not active_market:
        active_market = "ALL"
    if not active_market:
        return {"skipped": "market_closed"}

    from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
    policy = compute_toss_live_pilot_policy()
    if not policy.get("autonomous_mode"):
        return {"skipped": "autonomous_mode_disabled"}
    if policy.get("autonomous_kill_switch"):
        return {"skipped": "kill_switch_active"}

    state = _load_state()

    # 스로틀
    last_run = state.get("last_run", "")
    if not force and last_run:
        try:
            last_dt = datetime.fromisoformat(last_run)
            if (now - last_dt) < timedelta(minutes=_throttle_minutes()):
                return {"skipped": "throttled"}
        except ValueError:
            pass

    today = now.strftime("%Y-%m-%d")
    attempted_map = state.get("attempted", {})
    if state.get("attempted_date") != today:
        attempted_map = {}

    # 후보 선별
    try:
        ready, not_ready = select_ready_candidates(market=active_market)
    except Exception as e:
        log.warning("auto pipeline candidate fetch failed: %s", e)
        ready, not_ready = [], [{"symbol": "", "reason": f"candidate_fetch_failed: {e}"}]

    results: list[dict] = []
    for candidate in ready:
        if len(results) >= _MAX_ATTEMPTS_PER_RUN:
            break
        symbol = str(candidate.get("symbol") or candidate.get("ticker") or "")
        if not symbol or symbol in attempted_map:
            continue
        try:
            r = process_candidate(candidate, policy)
        except Exception as e:
            log.error("auto pipeline candidate error: %s %s", symbol, e)
            r = {"symbol": symbol, "stage": "error", "reason": str(e)[:200]}
        attempted_map[symbol] = {"at": now.strftime("%H:%M"), "stage": r.get("stage", ""), "verdict": r.get("verdict", "")}
        results.append(r)

    # retryable 주문 재시도 (state 공유 — 아래 _save_state에서 함께 저장)
    try:
        retry_summary = retry_retryable_orders(now=now, state=state)
    except Exception as e:
        log.warning("retry sweep failed: %s", e)
        retry_summary = {"retried": 0, "sent": 0, "exhausted": 0}

    # no_action 진단
    diagnosis: dict = {}
    if not results:
        if not ready:
            diagnosis = {
                "reason": "no_ready_candidates",
                "not_ready": not_ready[:10],
            }
        else:
            diagnosis = {"reason": "all_ready_candidates_already_attempted_today"}

    state.update({
        "last_run": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "attempted_date": today,
        "attempted": attempted_map,
        "active_market": active_market,
        "last_results": results,
        "no_action_diagnosis": diagnosis if diagnosis else None,
    })
    _save_state(state)

    pass_count = sum(1 for r in results if r.get("verdict") == "PASS")
    if results:
        log.info(
            "auto pipeline: %d attempted, %d PASS — %s",
            len(results), pass_count,
            "; ".join(f"{r['symbol']}:{r.get('verdict', r.get('stage'))}" for r in results),
        )

    return {
        "attempted": len(results),
        "pass_count": pass_count,
        "active_market": active_market,
        "results": results,
        "retry": retry_summary,
        "no_action_diagnosis": diagnosis or None,
    }
