"""실행 후보 한정 Red Team 분석 — read-only advisory staging.

이 모듈은 주문 후보 스냅샷과 공개 증거를 반증 관점에서 검토한다.
반환하는 PASS/REVIEW/BLOCK은 검토 신호일 뿐 주문 승인·취소·전송 신호가 아니다.
주문/브로커/운영 score 경로를 import하거나 호출하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Callable, Mapping
from urllib.parse import urlparse

KST = timezone(timedelta(hours=9))
log = logging.getLogger(__name__)

VERSION = "execution_candidate_red_team_v2"
VERDICTS = frozenset({"PASS", "REVIEW", "BLOCK"})
EXECUTABLE_BUCKETS = frozenset({"PASS_EXECUTE", "SMALL_PASS"})

_RED_TEAM_SCHEMA = {
    "type": "object",
    "properties": {
        "review_signal": {"type": "string", "enum": ["PASS", "REVIEW", "BLOCK"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "strongest_bear_case": {"type": "string"},
        "thesis_assumptions": {"type": "array", "items": {"type": "string"}},
        "disconfirming_evidence": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "red_lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "condition": {"type": "string"},
                    "status": {"type": "string", "enum": ["clear", "watch", "breached", "unknown"]},
                    "reason": {"type": "string"},
                },
                "required": ["condition", "status", "reason"],
            },
        },
        "scenarios": {
            "type": "object",
            "properties": {
                "bull": {"type": "string"},
                "base": {"type": "string"},
                "bear": {"type": "string"},
            },
            "required": ["bull", "base", "bear"],
        },
        "source_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "url": {"type": "string"},
                    "published_at": {"type": "string"},
                    "source_type": {"type": "string"},
                },
                "required": ["claim", "url", "published_at", "source_type"],
            },
        },
        "next_checks": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "review_signal", "confidence", "summary", "strongest_bear_case",
        "thesis_assumptions", "disconfirming_evidence", "missing_evidence",
        "red_lines", "scenarios", "source_evidence", "next_checks",
    ],
}

_SYSTEM_PROMPT = """너는 실제 주문 후보를 공격적으로 반증하는 독립 Red Team 투자 분석가다.
목표는 매수/매도 주장을 강화하는 것이 아니라 틀릴 가능성과 누락 증거를 찾는 것이다.

규칙:
1. 제공된 후보와 공개적으로 확인 가능한 최신 자료만 사용한다.
2. WebSearch를 사용했다면 주장마다 원문 URL과 게시일을 남긴다. 검색결과 요약 URL보다 기업 공시·거래소·규제기관·공식 IR 같은 1차 출처를 우선한다.
3. 근거가 부족하면 PASS나 BLOCK을 단정하지 말고 REVIEW를 반환한다.
4. BLOCK은 논지 무효화, 가격/손절 모순, 중대한 공시 반증처럼 실행 전 반드시 재검토해야 할 경우에만 사용한다.
5. PASS는 위험이 없다는 뜻이 아니라 현재 확인된 반증이 실행 논지를 깨지 못했다는 뜻이다.
6. 이 결과는 주문 승인·취소·매도 명령이 아니다. 주문 실행을 지시하는 문장을 쓰지 않는다.
7. 수치·날짜·사실을 지어내지 않는다. 확인할 수 없으면 missing_evidence에 기록한다.
8. 단일 JSON 객체로만 답한다."""


def _float(value: object, default: float = 0.0) -> float:
    try:
        raw = default if value in (None, "") else value
        return float(str(raw))
    except (TypeError, ValueError):
        return default


def _int(value: object, default: int = 0) -> int:
    try:
        raw = default if value in (None, "") else value
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return default


def _strings(value: object, *, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:500])
        if len(out) >= limit:
            break
    return out


def _valid_url(value: object) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_source_evidence(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in value[:20]:
        if not isinstance(row, Mapping):
            continue
        url = str(row.get("url") or "").strip()
        if not _valid_url(url) or url in seen:
            continue
        seen.add(url)
        out.append({
            "claim": str(row.get("claim") or "").strip()[:800],
            "url": url,
            "published_at": str(row.get("published_at") or "unknown").strip()[:40],
            "source_type": str(row.get("source_type") or "unknown").strip()[:80],
        })
    return out


def _normalize_red_lines(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for row in value[:20]:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("status") or "unknown").strip().lower()
        if status not in {"clear", "watch", "breached", "unknown"}:
            status = "unknown"
        condition = str(row.get("condition") or "").strip()
        if not condition:
            continue
        out.append({
            "condition": condition[:500],
            "status": status,
            "reason": str(row.get("reason") or "").strip()[:800],
        })
    return out


def _normalize_candidate(candidate: Mapping) -> dict:
    symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip()
    side = str(candidate.get("side") or "buy").lower().strip()
    limit_price = _float(candidate.get("limit_price") or candidate.get("entry_price") or candidate.get("price"))
    return {
        "symbol": symbol,
        "name": str(candidate.get("name") or symbol).strip()[:120],
        "side": side,
        "market": str(candidate.get("market") or "").upper().strip(),
        "quantity": _int(candidate.get("quantity")),
        "limit_price": limit_price,
        "current_price": _float(candidate.get("current_price") or candidate.get("price")),
        "stop_loss": _float(candidate.get("stop_loss")),
        "target_price": _float(candidate.get("target_price")),
        "invalidation": str(candidate.get("invalidation") or "").strip()[:800],
        "reason": str(candidate.get("reason") or candidate.get("decision_reason") or "").strip()[:1500],
        "score": _float(candidate.get("score") or candidate.get("score_total")),
        "risk_reward": _float(candidate.get("risk_reward") or candidate.get("rr_ratio")),
        "decision_bucket": str(candidate.get("decision_bucket") or "").strip(),
        "stock_agent_ready": candidate.get("stock_agent_ready"),
        "executable_now": candidate.get("executable_now"),
        "execution_status": str(candidate.get("execution_status") or "").strip(),
        "ai_berkshire_buy_block": bool(candidate.get("ai_berkshire_buy_block", False)),
        "ai_berkshire_buy_reason": str(candidate.get("ai_berkshire_buy_reason") or "").strip()[:500],
        "quote_age_sec": _int(candidate.get("quote_age_sec"), -1),
        "decision_ref": str(candidate.get("decision_ref") or candidate.get("source_prediction_id") or "").strip()[:160],
        "source_signal": str(candidate.get("source_signal") or "").strip()[:120],
        "risk_notes": _strings(candidate.get("risk_notes")),
    }


def _normalize_context(context: Mapping | None) -> dict:
    raw = context or {}
    portfolio_raw = raw.get("portfolio_risk")
    portfolio: Mapping = portfolio_raw if isinstance(portfolio_raw, Mapping) else {}
    return {
        "thesis": str(raw.get("thesis") or "").strip()[:2500],
        "catalysts": _strings(raw.get("catalysts")),
        "red_lines": _strings(raw.get("red_lines")),
        "market_context": str(raw.get("market_context") or "").strip()[:3000],
        "fundamental_context": str(raw.get("fundamental_context") or "").strip()[:3000],
        "technical_context": str(raw.get("technical_context") or "").strip()[:2000],
        "known_evidence": _strings(raw.get("known_evidence"), limit=30),
        "known_source_urls": [u for u in _strings(raw.get("source_urls"), limit=20) if _valid_url(u)],
        "event_days": _int(raw.get("days_to_event"), -1),
        "portfolio_risk": {
            "severity": str(portfolio.get("severity") or portfolio.get("overall_level") or "").lower(),
            "symbol_weight_pct": _float(portfolio.get("symbol_weight_pct")),
            "cluster_weight_pct": _float(portfolio.get("cluster_weight_pct")),
            "summary": str(portfolio.get("summary") or "").strip()[:1000],
        },
    }


def _review_id(candidate: Mapping, context: Mapping, as_of: str) -> str:
    seed = json.dumps(
        {"candidate": candidate, "context": context, "as_of": as_of},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "rt_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def deterministic_checks(candidate: Mapping, context: Mapping | None = None) -> dict:
    """후보 스냅샷의 명시적 모순과 누락을 검사한다.

    반환 신호도 advisory이며 주문 상태를 바꾸지 않는다.
    """
    c = _normalize_candidate(candidate)
    x = _normalize_context(context)
    blocks: list[str] = []
    reviews: list[str] = []
    warnings: list[str] = []

    if not c["symbol"]:
        blocks.append("symbol_missing")
    if c["side"] not in {"buy", "sell"}:
        blocks.append("side_invalid")
    if c["quantity"] <= 0:
        blocks.append("quantity_missing_or_zero")
    if c["limit_price"] <= 0:
        blocks.append("limit_price_missing_or_zero")

    if c["stock_agent_ready"] is False or c["executable_now"] is False:
        blocks.append("candidate_not_execution_ready")
    if c["decision_bucket"] and c["decision_bucket"] not in EXECUTABLE_BUCKETS:
        blocks.append(f"non_executable_bucket:{c['decision_bucket']}")
    if c["ai_berkshire_buy_block"] and c["side"] == "buy":
        blocks.append("ai_berkshire_buy_block")

    if c["side"] == "buy":
        if c["stop_loss"] <= 0 and not c["invalidation"]:
            reviews.append("invalidation_missing")
        if c["stop_loss"] > 0 and c["limit_price"] > 0 and c["stop_loss"] >= c["limit_price"]:
            blocks.append("stop_loss_not_below_buy_price")
        if c["target_price"] > 0 and c["limit_price"] > 0 and c["target_price"] <= c["limit_price"]:
            blocks.append("target_not_above_buy_price")

    if 0 < c["risk_reward"] < 1.2:
        blocks.append("risk_reward_below_1_2")
    elif 0 < c["risk_reward"] < 1.8:
        reviews.append("risk_reward_below_1_8")
    elif c["risk_reward"] <= 0:
        reviews.append("risk_reward_missing")

    if c["quote_age_sec"] > 3600:
        blocks.append("quote_older_than_1h")
    elif c["quote_age_sec"] > 900:
        reviews.append("quote_older_than_15m")
    elif c["quote_age_sec"] < 0:
        warnings.append("quote_age_unknown")

    thesis = x["thesis"] or c["reason"]
    if not thesis:
        reviews.append("thesis_missing")
    if not x["catalysts"]:
        reviews.append("catalysts_missing")
    if not x["red_lines"] and not c["invalidation"]:
        reviews.append("red_lines_missing")

    if 0 <= x["event_days"] <= 3:
        reviews.append("material_event_within_3d")

    pr = x["portfolio_risk"]
    if pr["severity"] in {"critical", "high"}:
        reviews.append(f"portfolio_cluster_risk:{pr['severity']}")
    if pr["symbol_weight_pct"] >= 25:
        reviews.append("single_symbol_weight_at_or_above_25pct")
    if pr["cluster_weight_pct"] >= 50:
        reviews.append("cluster_weight_at_or_above_50pct")

    if not c["decision_ref"]:
        warnings.append("decision_ref_missing")

    return {
        "blocks": list(dict.fromkeys(blocks)),
        "reviews": list(dict.fromkeys(reviews)),
        "warnings": list(dict.fromkeys(warnings)),
        "normalized_candidate": c,
        "normalized_context": x,
    }


def build_red_team_prompt(candidate: Mapping, context: Mapping | None = None, *, as_of: str) -> str:
    checks = deterministic_checks(candidate, context)
    payload = {
        "as_of": as_of,
        "candidate": checks["normalized_candidate"],
        "analysis_context": checks["normalized_context"],
        "deterministic_precheck": {
            "blocks": checks["blocks"],
            "reviews": checks["reviews"],
            "warnings": checks["warnings"],
        },
    }
    return (
        "다음 실행 후보를 Red Team 관점에서 검토해라. 강세 논리를 반복하지 말고 "
        "논지의 가장 약한 가정, 최신 반증, 공시 리스크, 밸류에이션/촉매/포트폴리오 "
        "집중도 모순을 찾아라. 가능하면 WebSearch로 1차 출처를 확인해라.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _default_ai_runner(prompt: str, *, model: str) -> str:
    from core.claude_cli import claude_cli

    return claude_cli(
        prompt=prompt,
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        timeout=300,
        json_schema=json.dumps(_RED_TEAM_SCHEMA, ensure_ascii=False),
        effort="high",
        allowed_tools="WebSearch",
    )


def _parse_ai_result(raw: object) -> tuple[dict, str | None]:
    if isinstance(raw, Mapping):
        data = dict(raw)
    else:
        text = str(raw or "").strip()
        if not text:
            return {}, "ai_empty_response"
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return {}, "ai_invalid_json"
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}, "ai_invalid_json"
    if not isinstance(data, dict):
        return {}, "ai_result_not_object"
    verdict = str(data.get("review_signal") or "").upper()
    if verdict not in VERDICTS:
        return {}, "ai_verdict_invalid"
    return data, None


def _normalize_ai_result(data: Mapping) -> dict:
    scenarios_raw = data.get("scenarios")
    scenarios: Mapping = scenarios_raw if isinstance(scenarios_raw, Mapping) else {}
    return {
        "review_signal": str(data.get("review_signal") or "REVIEW").upper(),
        "confidence": max(0, min(100, _int(data.get("confidence"), 0))),
        "summary": str(data.get("summary") or "").strip()[:1500],
        "strongest_bear_case": str(data.get("strongest_bear_case") or "").strip()[:2000],
        "thesis_assumptions": _strings(data.get("thesis_assumptions"), limit=20),
        "disconfirming_evidence": _strings(data.get("disconfirming_evidence"), limit=20),
        "missing_evidence": _strings(data.get("missing_evidence"), limit=20),
        "red_lines": _normalize_red_lines(data.get("red_lines")),
        "scenarios": {
            "bull": str(scenarios.get("bull") or "").strip()[:1000],
            "base": str(scenarios.get("base") or "").strip()[:1000],
            "bear": str(scenarios.get("bear") or "").strip()[:1000],
        },
        "source_evidence": _normalize_source_evidence(data.get("source_evidence")),
        "next_checks": _strings(data.get("next_checks"), limit=20),
    }


def evaluate_execution_candidate(
    candidate: Mapping,
    context: Mapping | None = None,
    *,
    ai_runner: Callable[..., object] | None = None,
    model: str = "opus",
    as_of: datetime | None = None,
    run_ai: bool = True,
) -> dict:
    """실행 후보 1건을 분석하고 read-only staging 레코드를 반환한다."""
    now = as_of or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    as_of_text = now.astimezone(KST).isoformat(timespec="seconds")
    checks = deterministic_checks(candidate, context)
    c = checks["normalized_candidate"]
    x = checks["normalized_context"]

    ai_data: dict = {}
    ai_error: str | None = None
    if run_ai:
        prompt = build_red_team_prompt(c, x, as_of=as_of_text)
        runner = ai_runner or _default_ai_runner
        try:
            raw = runner(prompt, model=model)
            parsed, ai_error = _parse_ai_result(raw)
            if parsed:
                ai_data = _normalize_ai_result(parsed)
        except Exception as exc:
            log.warning("execution red team AI failed for %s: %s", c["symbol"], exc)
            ai_error = "ai_runner_failed"
    else:
        ai_error = "ai_disabled"

    if checks["blocks"]:
        final_verdict = "BLOCK"
        verdict_reason = "deterministic_block"
    elif not ai_data:
        final_verdict = "REVIEW"
        verdict_reason = ai_error or "ai_unavailable"
    else:
        requested = ai_data["review_signal"]
        sources = ai_data["source_evidence"]
        breached = [r for r in ai_data["red_lines"] if r["status"] == "breached"]
        if requested == "BLOCK":
            if ai_data["confidence"] >= 70 and len(sources) >= 2 and (breached or ai_data["disconfirming_evidence"]):
                final_verdict = "BLOCK"
                verdict_reason = "ai_block_evidence_threshold_met"
            else:
                final_verdict = "REVIEW"
                verdict_reason = "ai_block_downgraded_insufficient_evidence"
        elif requested == "PASS":
            if checks["reviews"]:
                final_verdict = "REVIEW"
                verdict_reason = "deterministic_review_not_overridden"
            elif not sources or not ai_data["strongest_bear_case"]:
                final_verdict = "REVIEW"
                verdict_reason = "ai_pass_downgraded_missing_counterevidence"
            else:
                final_verdict = "PASS"
                verdict_reason = "ai_pass_with_counterevidence"
        else:
            final_verdict = "REVIEW"
            verdict_reason = "ai_review"

    return {
        "version": VERSION,
        "review_id": _review_id(c, x, as_of_text),
        "decision_ref": c["decision_ref"] or None,
        "traceability_status": "direct" if c["decision_ref"] else "missing_decision_ref",
        "symbol": c["symbol"],
        "name": c["name"],
        "side": c["side"],
        "generated_at": as_of_text,
        "review_signal": final_verdict,
        "verdict_reason": verdict_reason,
        "confidence": ai_data.get("confidence", 0),
        "summary": ai_data.get("summary", "AI 분석 미실행 또는 실패 — 수동 검토 필요"),
        "strongest_bear_case": ai_data.get("strongest_bear_case", ""),
        "thesis_assumptions": ai_data.get("thesis_assumptions", []),
        "disconfirming_evidence": ai_data.get("disconfirming_evidence", []),
        "missing_evidence": ai_data.get("missing_evidence", []),
        "red_lines": ai_data.get("red_lines", []),
        "scenarios": ai_data.get("scenarios", {"bull": "", "base": "", "bear": ""}),
        "source_evidence": ai_data.get("source_evidence", []),
        "next_checks": ai_data.get("next_checks", []),
        "deterministic_checks": {
            "blocks": checks["blocks"],
            "reviews": checks["reviews"],
            "warnings": checks["warnings"],
        },
        "candidate_snapshot": c,
        "analysis_context": x,
        "ai": {
            "requested": run_ai,
            "model": model if run_ai else None,
            "error": ai_error,
            "web_search_allowed": bool(run_ai),
        },
        "data_quality": {
            "ai_result_available": bool(ai_data),
            "valid_source_count": len(ai_data.get("source_evidence", [])),
            "missing_evidence_count": len(ai_data.get("missing_evidence", [])),
            "deterministic_warning_count": len(checks["warnings"]),
        },
        "review_only": True,
        "operational_decision_unchanged": True,
        "advisory_only": True,
        "order_signal": False,
        "order_side_effects": False,
        "can_approve_order": False,
        "can_cancel_order": False,
        "can_send_order": False,
        "note": "PASS/REVIEW/BLOCK은 독립 검토 신호이며 기존 주문 게이트를 변경하지 않음",
    }


def validate_staging_record(record: Mapping) -> list[str]:
    """저장 전 안전 계약을 검증하고 오류 목록을 반환한다."""
    errors: list[str] = []
    if record.get("version") != VERSION:
        errors.append("version_invalid")
    if record.get("review_signal") not in VERDICTS:
        errors.append("verdict_invalid")
    if not record.get("review_id"):
        errors.append("review_id_missing")
    if not record.get("symbol"):
        errors.append("symbol_missing")
    for key in (
        "review_only", "operational_decision_unchanged", "advisory_only",
        "order_side_effects", "order_signal",
        "can_approve_order", "can_cancel_order", "can_send_order",
    ):
        expected = key in {"review_only", "operational_decision_unchanged", "advisory_only"}
        if record.get(key) is not expected:
            errors.append(f"unsafe_contract:{key}")
    sources = record.get("source_evidence") or []
    if any(not _valid_url(s.get("url")) for s in sources if isinstance(s, Mapping)):
        errors.append("source_url_invalid")
    return errors
