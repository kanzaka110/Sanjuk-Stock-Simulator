"""core/toss_autonomous_finalizer.py

Hermes PASS → 자동 주문 실행 모듈.

[구조]
  1. policy 로드 → autonomous_mode=True, kill_switch=False 확인
  2. pilot record 조회
  3. Hermes verification PASS + 미만료 확인
  4. can_send_live_pilot_order() 가드 체인
  5. transport 해소 + dispatch
  6. ledger/events 기록
  7. Telegram 결과 보고 (승인 버튼 없음)

[안전]
- autonomous_mode=False → no-op
- kill_switch=True → 전체 차단
- Hermes PASS 없으면 → 절대 주문 안 함
- 모든 에러 → 호출부에 전파하지 않음 (fail-safe)

[금지]
- 민감정보(key/secret/accountNo) 출력 금지
- 삼성증권 자동화 금지 (digit-only 심볼 차단)
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def try_autonomous_finalize(pilot_id: str) -> dict:
    """Hermes PASS 후 자율 주문 실행 시도.

    autonomous mode가 아니면 즉시 no-op 반환.
    모든 예외를 내부에서 잡아 호출부 안전 보장.

    Returns:
        {"ok": bool, "action": str, "live_order_sent": bool, ...}
    """
    try:
        return _finalize_impl(pilot_id)
    except Exception as e:
        log.error("autonomous finalize unexpected error: pilot_id=%s %s", pilot_id, e)
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": f"unexpected_error: {e}",
        }


def _finalize_impl(pilot_id: str) -> dict:
    """실제 자율 실행 로직."""
    from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
    from core.toss_live_pilot_adapter import (
        can_send_live_pilot_order,
        dispatch_toss_order_live,
    )
    from core.toss_live_pilot_ledger import (
        list_live_pilot_records,
        record_live_send_blocked,
        record_live_sent,
        record_live_send_failed,
        record_live_send_retryable,
    )
    from core.toss_live_pilot_verification import is_verification_passed
    from core.toss_live_pilot_telegram import resolve_live_transport_for_confirm

    # 1. policy
    policy = compute_toss_live_pilot_policy()

    if not policy.get("autonomous_mode"):
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": "autonomous_mode_disabled",
            "skipped": True,
        }

    if policy.get("autonomous_kill_switch"):
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": "autonomous_kill_switch_active",
        }

    # 2. pilot record
    records = list_live_pilot_records(limit=200)
    matched = [r for r in records if r.get("pilot_id") == pilot_id]
    if not matched:
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": "pilot_id_not_found",
        }
    rec = matched[0]

    # 이미 처리된 경우 스킵
    if rec.get("status") in ("live_sent", "cancelled", "live_send_failed", "live_send_retryable"):
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": f"already_processed: {rec.get('status')}",
            "skipped": True,
        }

    # 3. Hermes verification PASS
    verif_ok, verif_reasons, verif_rec = is_verification_passed(pilot_id)
    verification_id = verif_rec.get("verification_id", "") if verif_rec else ""

    if not verif_ok:
        _record_event(
            pilot_id=pilot_id,
            event_type="autonomous_blocked_hermes",
            status="live_send_blocked",
            verification_id=verification_id,
            reason="hermes_verification_required",
            rec=rec,
            policy=policy,
        )
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": "hermes_verification_required",
            "verif_reasons": verif_reasons,
        }

    # 4. policy gates
    if not policy.get("live_order_allowed") or policy.get("adapter_status") != "enabled":
        _record_event(
            pilot_id=pilot_id,
            event_type="autonomous_blocked_policy",
            status="live_send_blocked",
            verification_id=verification_id,
            reason="live_pilot_conditions_not_met",
            rec=rec,
            policy=policy,
        )
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": "live_pilot_conditions_not_met",
        }

    # 5. can_send guards
    preview_stub = {
        "ok": rec.get("status") not in ("blocked", "cancelled"),
        "symbol": rec.get("symbol", ""),
        "side": rec.get("side", "buy"),
        "quantity": rec.get("quantity", 0),
        "limit_price": rec.get("limit_price", 0),
        "estimated_amount_krw": rec.get("estimated_amount_krw", 0),
        "blocks": rec.get("blocks", []),
        "live_order_sent": bool(rec.get("live_order_sent")),
        "stop_loss": rec.get("stop_loss"),
        "invalidation": rec.get("invalidation"),
    }
    payload_result_stub = {
        "ok": preview_stub["ok"],
        "live_order_sent": preview_stub["live_order_sent"],
    }

    can_send, guard_reasons = can_send_live_pilot_order(policy, preview_stub, payload_result_stub)
    if not can_send:
        try:
            record_live_send_blocked(pilot_id, guard_reasons)
        except Exception as e:
            log.warning("autonomous blocked ledger failed: %s", e)

        _record_event(
            pilot_id=pilot_id,
            event_type="autonomous_blocked_guard",
            status="live_send_blocked",
            verification_id=verification_id,
            reason=f"guard_failed: {'; '.join(guard_reasons[:3])}",
            rec=rec,
            policy=policy,
        )

        _send_result_telegram(
            "blocked", rec, guard_reasons=guard_reasons,
        )

        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": "guard_failed",
            "guard_reasons": guard_reasons,
        }

    # 6. transport + dispatch
    payload = {
        "symbol": preview_stub["symbol"],
        "side": preview_stub["side"],
        "order_type": "limit",
        "quantity": preview_stub["quantity"],
        "limit_price": preview_stub["limit_price"],
        "estimated_amount_krw": preview_stub["estimated_amount_krw"],
    }
    transport = resolve_live_transport_for_confirm(policy)
    dispatch_result = dispatch_toss_order_live(payload, policy, transport=transport)

    # 7. ledger + events + telegram
    if dispatch_result.get("live_order_sent"):
        try:
            record_live_sent(
                pilot_id,
                broker_order_id=dispatch_result.get("broker_order_id", ""),
                payload_hash=dispatch_result.get("payload_hash", ""),
            )
        except Exception as e:
            log.warning("autonomous live_sent ledger failed: %s", e)

        _record_event(
            pilot_id=pilot_id,
            event_type="autonomous_live_sent",
            status="live_sent",
            verification_id=verification_id,
            reason="autonomous_execution",
            rec=rec,
            policy=policy,
            live_order_sent=True,
            broker_order_id=dispatch_result.get("broker_order_id", ""),
            broker_order_status=dispatch_result.get("broker_order_status", ""),
            filled_quantity=float(dispatch_result.get("filled_quantity") or 0),
            filled_price=float(dispatch_result.get("filled_price") or 0),
        )

        _send_result_telegram("sent", rec, dispatch_result=dispatch_result)

        return {
            "ok": True,
            "action": "autonomous_finalize",
            "live_order_sent": True,
            "broker_order_id": dispatch_result.get("broker_order_id", ""),
            "broker_order_status": dispatch_result.get("broker_order_status", ""),
        }
    else:
        fail_reason = dispatch_result.get("reason", "dispatch_failed") or "dispatch_failed"
        error_body = dispatch_result.get("error_body", "")
        failure_reason = (
            dispatch_result.get("failure_reason")
            or fail_reason
            or "dispatch_failed"
        )
        fail_detail = f"{failure_reason}: {error_body}" if error_body else failure_reason
        retryable = _is_retryable_dispatch_failure(failure_reason, error_body)
        try:
            recorder = record_live_send_retryable if retryable else record_live_send_failed
            recorder(
                pilot_id,
                failure_reason=fail_detail[:500],
                payload_hash=dispatch_result.get("payload_hash", ""),
            )
        except Exception as e:
            log.warning("autonomous live_send_failed ledger failed: %s", e)

        event_type = "autonomous_send_retryable" if retryable else "autonomous_send_failed"
        event_status = "live_send_retryable" if retryable else "live_send_failed"
        _record_event(
            pilot_id=pilot_id,
            event_type=event_type,
            status=event_status,
            verification_id=verification_id,
            reason=fail_detail[:500],
            rec=rec,
            policy=policy,
        )

        _send_result_telegram(
            "failed", rec,
            failure_reason=fail_detail[:200],
        )

        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": fail_reason,
            "error_body": error_body[:300] if error_body else "",
        }


def _is_retryable_dispatch_failure(reason: str, error_body: str = "") -> bool:
    """일시적 transport/account/API 실패는 terminal failed로 소비하지 않는다."""
    text = f"{reason} {error_body}".lower()
    retryable_tokens = (
        "dispatch_failed",
        "transport_exception",
        "network_error",
        "account_unavailable",
        "token_unavailable",
        "http_401",
        "http_429",
        "rate limit",
        "timeout",
        "temporarily",
    )
    return any(tok in text for tok in retryable_tokens)


# ── 이벤트 기록 ──────────────────────────────────────────────────

def _record_event(
    *,
    pilot_id: str,
    event_type: str,
    status: str,
    verification_id: str,
    reason: str,
    rec: dict,
    policy: dict,
    **extra,
) -> None:
    """events DB에 자율실행 이벤트 기록."""
    try:
        from core.toss_live_pilot_events import record_event
        record_event(
            pilot_id=pilot_id,
            event_type=event_type,
            status=status,
            preview_id=pilot_id,
            verification_id=verification_id,
            reason=reason,
            message=f"autonomous {event_type}",
            symbol=rec.get("symbol", ""),
            side=rec.get("side", "buy"),
            quantity=int(rec.get("quantity") or 0),
            limit_price=float(rec.get("limit_price") or 0),
            estimated_amount_krw=float(rec.get("estimated_amount_krw") or 0),
            adapter_status=policy.get("adapter_status", "disabled"),
            **extra,
        )
    except Exception as e:
        log.warning("autonomous event record failed: %s", e)


# ── Telegram 결과 보고 ────────────────────────────────────────────

def _send_result_telegram(
    result_type: str,
    rec: dict,
    *,
    dispatch_result: dict | None = None,
    guard_reasons: list[str] | None = None,
    failure_reason: str = "",
) -> bool:
    """자율실행 결과를 Telegram으로 보고 (승인 버튼 없음)."""
    symbol = rec.get("symbol", "")
    side = str(rec.get("side", "buy")).upper()
    qty = rec.get("quantity", 0)
    price = rec.get("limit_price", 0)

    if result_type == "sent" and dispatch_result:
        broker_id = dispatch_result.get("broker_order_id", "미확인")
        broker_status = dispatch_result.get("broker_order_status", "확인대기")
        filled_qty = float(dispatch_result.get("filled_quantity") or 0)
        text = (
            f"[자율실행 체결] {symbol} {side} {qty}주 @ {price}\n"
            f"broker_id={broker_id}\n"
            f"broker_status={broker_status} filled_qty={filled_qty:g}\n"
            f"live_order_sent=true"
        )
    elif result_type == "blocked":
        reasons_str = "; ".join((guard_reasons or [])[:3])
        text = (
            f"[자율실행 차단] {symbol} {side}\n"
            f"사유: {reasons_str}\n"
            f"live_order_sent=false"
        )
    elif result_type == "failed":
        text = (
            f"[자율실행 실패] {symbol} {side}\n"
            f"사유: {failure_reason}\n"
            f"live_order_sent=false"
        )
    else:
        return False

    try:
        from core.toss_live_pilot_telegram import send_autonomous_result_message
        return send_autonomous_result_message(text)
    except Exception as e:
        log.warning("autonomous telegram send failed: %s", e)
        return False
