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
import threading
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
_exit_dispatch_local = threading.local()


def _acquire_active_exit_dispatch_lock(rec: dict) -> dict:
    from core.toss_exit_execution_intent import (
        acquire_exit_dispatch_lock,
        exit_decision_ref_matches,
        is_exit_decision_ref,
    )

    decision_ref = rec.get("decision_ref")
    side = str(rec.get("side") or "").lower()
    if side == "sell" and (
        not is_exit_decision_ref(decision_ref)
        or not exit_decision_ref_matches(
            decision_ref, rec.get("symbol"), datetime.now(KST),
        )
    ):
        return {"ok": False, "managed": True, "reason": "invalid_exit_decision_ref"}
    managed = side == "sell"
    if not managed:
        return {"ok": True, "managed": False}
    if getattr(_exit_dispatch_local, "lock", None) is not None:
        return {"ok": False, "managed": True, "reason": "exit_intent_lock_reentrant"}
    lock = acquire_exit_dispatch_lock(str(decision_ref))
    if lock.get("ok") is not True:
        return {
            "ok": False,
            "managed": True,
            "reason": str(lock.get("reason") or "exit_intent_state_unavailable"),
        }
    _exit_dispatch_local.lock = lock
    return {"ok": True, "managed": True}


def _release_active_exit_dispatch_lock() -> None:
    lock = getattr(_exit_dispatch_local, "lock", None)
    if lock is None:
        return
    try:
        from core.toss_exit_execution_intent import release_exit_dispatch_lock
        release_exit_dispatch_lock(lock)
    finally:
        try:
            del _exit_dispatch_local.lock
        except AttributeError:
            pass


def _reconcile_prior_exit_intent(prior_pilot_id: str, records: list[dict]) -> dict:
    prior = next((r for r in records if r.get("pilot_id") == prior_pilot_id), None)
    if prior is not None and (
        prior.get("status") == "live_sent" or prior.get("live_order_sent") is True
    ):
        return {"ok": True, "sent": True, "source": "ledger"}

    try:
        from core.toss_live_order_http import list_orders
        responses = [list_orders("OPEN"), list_orders("CLOSED")]
    except Exception:
        return {"ok": False, "sent": False, "source": "broker_unavailable"}
    for expected_status, response in zip(("OPEN", "CLOSED"), responses):
        if (
            type(response) is not dict
            or response.get("ok") is not True
            or response.get("status") != expected_status
            or response.get("complete") is not True
        ):
            return {"ok": False, "sent": False, "source": "broker_unavailable"}
        orders = response.get("orders")
        if type(orders) is not list:
            return {"ok": False, "sent": False, "source": "broker_unavailable"}
        for order in orders:
            if type(order) is dict and order.get("client_order_id") == prior_pilot_id:
                broker_order_id = next((
                    str(order.get(key))
                    for key in ("broker_order_id", "order_id", "id")
                    if type(order.get(key)) is str and order.get(key)
                ), "")
                return {
                    "ok": True,
                    "sent": True,
                    "source": "broker",
                    "broker_order_id": broker_order_id,
                }
    return {"ok": True, "sent": False, "source": "broker"}


def _converge_reconciled_live_sent(pilot_id: str, broker_order_id: str = "") -> bool:
    try:
        from core.toss_live_pilot_ledger import record_live_sent
        record_live_sent(pilot_id, broker_order_id=broker_order_id)
        return True
    except Exception as exc:
        log.warning(
            "reconciled live_sent ledger convergence failed: error_type=%s",
            type(exc).__name__,
        )
        return False


def _claim_exit_intent(rec: dict, pilot_id: str, records: list[dict]) -> dict:
    from core.toss_exit_execution_intent import (
        claim_exit_intent,
        is_exit_decision_ref,
        mark_exit_intent_sent,
        takeover_exit_intent,
    )

    raw_decision_ref = rec.get("decision_ref")
    side = str(rec.get("side") or "").lower()
    if side != "sell":
        return {"ok": True, "managed": False, "decision_ref": ""}
    if not is_exit_decision_ref(raw_decision_ref):
        return {
            "ok": False,
            "managed": True,
            "decision_ref": "",
            "reason": "invalid_exit_decision_ref",
        }
    decision_ref = str(raw_decision_ref)
    claim = claim_exit_intent(decision_ref, pilot_id)
    if claim.get("reason") == "exit_intent_already_sent":
        _converge_reconciled_live_sent(str(claim.get("prior_pilot_id") or ""))
    if claim.get("reason") != "exit_intent_reconcile_required":
        return {**claim, "managed": True, "decision_ref": decision_ref}

    prior_pilot_id = str(claim.get("prior_pilot_id") or "")
    prior_decision_ref = str(claim.get("prior_decision_ref") or "")
    prior_updated_at = str(claim.get("prior_updated_at") or "")
    reconciled = _reconcile_prior_exit_intent(prior_pilot_id, records)
    if reconciled.get("ok") is not True:
        return {
            "ok": False,
            "managed": True,
            "decision_ref": decision_ref,
            "reason": "exit_intent_reconcile_unavailable",
        }
    if reconciled.get("sent") is True:
        mark_exit_intent_sent(prior_decision_ref, prior_pilot_id)
        _converge_reconciled_live_sent(
            prior_pilot_id,
            broker_order_id=str(reconciled.get("broker_order_id") or ""),
        )
        return {
            "ok": False,
            "managed": True,
            "decision_ref": decision_ref,
            "reason": "exit_intent_already_sent",
        }
    takeover = takeover_exit_intent(
        decision_ref,
        prior_pilot_id,
        pilot_id,
        expected_decision_ref=prior_decision_ref,
        expected_updated_at=prior_updated_at,
    )
    return {**takeover, "managed": True, "decision_ref": decision_ref}


def try_autonomous_finalize(pilot_id: str, allow_retry: bool = False) -> dict:
    """Hermes PASS 후 자율 주문 실행 시도.

    autonomous mode가 아니면 즉시 no-op 반환.
    모든 예외를 내부에서 잡아 호출부 안전 보장.
    allow_retry=True면 live_send_retryable 상태도 재실행 허용 (retry sweep 전용).

    Returns:
        {"ok": bool, "action": str, "live_order_sent": bool, ...}
    """
    previous_exit_lock = getattr(_exit_dispatch_local, "lock", None)
    try:
        return _finalize_impl(pilot_id, allow_retry=allow_retry)
    except Exception as e:
        log.error("autonomous finalize unexpected error: pilot_id=%s %s", pilot_id, e)
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": f"unexpected_error: {e}",
        }
    finally:
        # 재진입 차단된 내부 호출이 외부 호출의 lock을 풀면 fencing이 깨진다.
        if previous_exit_lock is None:
            _release_active_exit_dispatch_lock()


def _finalize_impl(pilot_id: str, allow_retry: bool = False) -> dict:
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

    # 이미 처리된 경우 스킵 (retry sweep은 live_send_retryable 재실행 허용)
    _skip_statuses = ("live_sent", "cancelled", "live_send_failed", "live_send_retryable")
    if allow_retry:
        _skip_statuses = ("live_sent", "cancelled", "live_send_failed")
    if rec.get("status") in _skip_statuses:
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

    prior_422 = _prior_http_422_failure_today(records, rec, pilot_id)
    if prior_422:
        try:
            record_live_send_blocked(pilot_id, [prior_422])
        except Exception as e:
            log.warning("autonomous prior-422 block ledger failed: %s", e)
        _record_event(
            pilot_id=pilot_id,
            event_type="autonomous_blocked_guard",
            status="live_send_blocked",
            verification_id=verification_id,
            reason=prior_422,
            rec=rec,
            policy=policy,
        )
        _send_result_telegram("blocked", rec, guard_reasons=[prior_422])
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": "prior_http_422_today",
            "guard_reasons": [prior_422],
            "skipped": True,
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

    # 6. BUY exact quality row — 마지막 pre-dispatch 공통 경계
    if str(preview_stub.get("side") or "").lower() == "buy":
        try:
            from core.toss_quality_gate import validate_execution_quality_decision
            quality_check = validate_execution_quality_decision(rec, pilot_id=pilot_id)
        except Exception as exc:
            log.warning(
                "autonomous quality last-mile check failed: error_type=%s",
                type(exc).__name__,
            )
            quality_check = {"ok": False, "reason": "quality_decision_unavailable"}
        if not quality_check.get("ok"):
            quality_reason = str(
                quality_check.get("reason") or "quality_decision_unavailable"
            )
            try:
                record_live_send_blocked(pilot_id, [quality_reason])
            except Exception as exc:
                log.warning(
                    "autonomous quality block ledger failed: error_type=%s",
                    type(exc).__name__,
                )
            _record_event(
                pilot_id=pilot_id,
                event_type="autonomous_blocked_quality",
                status="live_send_blocked",
                verification_id=verification_id,
                reason=quality_reason,
                rec=rec,
                policy=policy,
            )
            return {
                "ok": False,
                "action": "autonomous_finalize",
                "live_order_sent": False,
                "reason": quality_reason,
            }

    # 7. shared exit intent → transport + dispatch
    payload = {
        "symbol": preview_stub["symbol"],
        "side": preview_stub["side"],
        "order_type": "limit",
        "quantity": preview_stub["quantity"],
        "limit_price": preview_stub["limit_price"],
        "estimated_amount_krw": preview_stub["estimated_amount_krw"],
        "client_order_id": pilot_id,
        "pilot_id": pilot_id,
    }
    exit_lock = _acquire_active_exit_dispatch_lock(rec)
    if exit_lock.get("ok") is not True:
        reason = str(exit_lock.get("reason") or "exit_intent_state_unavailable")
        _record_event(
            pilot_id=pilot_id,
            event_type="autonomous_blocked_exit_intent",
            status="live_send_blocked",
            verification_id=verification_id,
            reason=reason,
            rec=rec,
            policy=policy,
        )
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": reason,
        }
    exit_intent = _claim_exit_intent(rec, pilot_id, records)
    if exit_intent.get("ok") is not True:
        reason = str(exit_intent.get("reason") or "exit_intent_blocked")
        _record_event(
            pilot_id=pilot_id,
            event_type="autonomous_blocked_exit_intent",
            status="live_send_blocked",
            verification_id=verification_id,
            reason=reason,
            rec=rec,
            policy=policy,
        )
        return {
            "ok": False,
            "action": "autonomous_finalize",
            "live_order_sent": False,
            "reason": reason,
        }
    try:
        transport = resolve_live_transport_for_confirm(policy)
    except Exception as exc:
        log.warning(
            "autonomous transport resolution failed: error_type=%s",
            type(exc).__name__,
        )
        raw_dispatch_result = {
            "ok": False,
            "live_order_sent": False,
            "blocked": True,
            "transport_status": "live_send_blocked",
            "reason": "transport_resolution_failed",
            "failure_reason": "transport_resolution_failed",
        }
    else:
        try:
            raw_dispatch_result = dispatch_toss_order_live(
                payload, policy, transport=transport,
            )
        except Exception as exc:
            log.error(
                "autonomous dispatch raised — outcome ambiguous: error_type=%s",
                type(exc).__name__,
            )
            raw_dispatch_result = {
                "ok": False,
                "live_order_sent": False,
                "blocked": False,
                "transport_status": "live_send_ambiguous",
                "reason": "transport_exception",
                "failure_reason": "transport_exception",
            }
    dispatch_contract_valid = bool(
        type(raw_dispatch_result) is dict
        and type(raw_dispatch_result.get("ok")) is bool
        and type(raw_dispatch_result.get("live_order_sent")) is bool
        and not (
            raw_dispatch_result.get("ok") is False
            and raw_dispatch_result.get("live_order_sent") is True
        )
    )
    dispatch_sent = bool(
        dispatch_contract_valid
        and raw_dispatch_result.get("ok") is True
        and raw_dispatch_result.get("live_order_sent") is True
    )
    dispatch_definitively_unsent = bool(
        dispatch_contract_valid
        and raw_dispatch_result.get("live_order_sent") is False
        and (
            raw_dispatch_result.get("blocked") is True
            or raw_dispatch_result.get("transport_status") == "live_send_blocked"
        )
    )
    if dispatch_contract_valid:
        dispatch_result = raw_dispatch_result
    else:
        dispatch_result = {
            "ok": False,
            "live_order_sent": False,
            "reason": "invalid_dispatch_result",
            "failure_reason": "invalid_dispatch_result",
        }

    # 7. ledger + events + telegram
    if dispatch_sent:
        if exit_intent.get("managed") is True:
            from core.toss_exit_execution_intent import mark_exit_intent_sent
            marked = mark_exit_intent_sent(exit_intent["decision_ref"], pilot_id)
            if marked.get("ok") is not True:
                log.error("exit intent sent-state persist failed: pilot_id=%s", pilot_id)
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
        if exit_intent.get("managed") is True and dispatch_definitively_unsent:
            from core.toss_exit_execution_intent import release_exit_intent
            released = release_exit_intent(exit_intent["decision_ref"], pilot_id)
            if released.get("ok") is not True:
                log.error("exit intent release failed: pilot_id=%s", pilot_id)
        elif exit_intent.get("managed") is True:
            # 모순/비bool 결과는 실제 전송 여부를 확정할 수 없다. claim을 유지해
            # lease 뒤 exact ledger/broker reconciliation으로만 takeover를 허용한다.
            log.error("ambiguous dispatch result — exit intent retained: pilot_id=%s", pilot_id)
        fail_reason = dispatch_result.get("reason", "dispatch_failed") or "dispatch_failed"
        error_body = dispatch_result.get("error_body", "")
        order_request_preview = (
            dispatch_result.get("order_request_preview")
            or dispatch_result.get("request_preview")
            or ""
        )
        failure_reason = (
            dispatch_result.get("failure_reason")
            or fail_reason
            or "dispatch_failed"
        )
        fail_detail = f"{failure_reason}: {error_body}" if error_body else failure_reason
        cash_shortage = _is_insufficient_buying_power_failure(failure_reason, error_body)
        if cash_shortage:
            fail_detail = f"cash_blocked_rebalance_needed: {fail_detail}"
        retryable = _is_retryable_dispatch_failure(failure_reason, error_body)
        reconcile_required = bool(
            exit_intent.get("managed") is True and not dispatch_definitively_unsent
        )
        if reconcile_required:
            # Blind resend는 claim이 막고, retry sweep은 lease 뒤 broker 원장
            # reconciliation에 다시 진입할 수 있도록 terminal 소비하지 않는다.
            retryable = True
            fail_detail = f"reconcile_required: {fail_detail}"
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
            error_body=error_body,
            order_request_preview=order_request_preview,
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
            "failure_class": "cash_blocked_rebalance_needed" if cash_shortage else fail_reason,
            "rebalance_needed": cash_shortage,
            "cash_blocked": cash_shortage,
            "error_body": error_body[:300] if error_body else "",
            "order_request_preview": order_request_preview,
        }


def _is_insufficient_buying_power_failure(reason: str, error_body: str = "") -> bool:
    """True when broker says cash/buying power is insufficient.

    This is not transient retry noise. A good candidate should move to
    sizing/rebalance planning instead of blind retry.
    """
    text = f"{reason} {error_body}".lower()
    tokens = (
        "insufficient-buying-power",
        "매수가능금액이 부족",
        "not enough buying power",
        "insufficient buying power",
    )
    return any(tok in text for tok in tokens)


def _is_retryable_dispatch_failure(reason: str, error_body: str = "") -> bool:
    """일시적 transport/account/API 실패는 terminal failed로 소비하지 않는다.

    단, 매수가능금액 부족은 네트워크/계좌 일시 장애가 아니다. 같은 주문을
    재시도해도 반복 실패하므로 sizing/rebalance 대상으로 남긴다.

    POST 401(auth_ambiguous 포함)도 맹목 재시도 금지: POST가 브로커에 일부
    도달했을 가능성을 배제하지 못해 재전송은 중복 주문 위험이다. transport가
    원장 대조까지 끝낸 terminal 판정이므로 여기서 되살리지 않는다.
    token_unavailable/account_unavailable은 POST 자체가 안 나간 차단이라
    bounded retry 후보로 유지한다.
    """
    text = f"{reason} {error_body}".lower()
    if _is_insufficient_buying_power_failure(reason, error_body):
        return False
    if "auth_ambiguous" in text or "http_401" in text:
        return False
    retryable_tokens = (
        "dispatch_failed",
        "transport_resolution_failed",
        "transport_exception",
        "network_error",
        "account_unavailable",
        "token_unavailable",
        "http_429",
        "rate limit",
        "timeout",
        "temporarily",
    )
    return any(tok in text for tok in retryable_tokens)


def _prior_http_422_failure_today(records: list[dict], rec: dict, current_pilot_id: str) -> str:
    """Same symbol/side http_422 failed once today -> do not hammer Toss again."""
    symbol = str(rec.get("symbol") or "").strip()
    side = str(rec.get("side") or "buy").lower()
    if not symbol:
        return ""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    for row in records:
        if row.get("pilot_id") == current_pilot_id:
            continue
        if str(row.get("symbol") or "").strip() != symbol:
            continue
        if str(row.get("side") or "buy").lower() != side:
            continue
        if str(row.get("created_at") or "")[:10] != today:
            continue
        if str(row.get("status") or "") != "live_send_failed":
            continue
        text = " ".join(str(row.get(k) or "") for k in ("failure_reason", "reason", "message"))
        if "http_422" in text.lower():
            return f"prior_http_422_today: {symbol}/{side}"
    return ""


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
            decision_ref=rec.get("decision_ref", ""),
            reason=reason,
            message=f"autonomous {event_type}",
            symbol=rec.get("symbol", ""),
            side=rec.get("side", "buy"),
            quantity=int(rec.get("quantity") or 0),
            limit_price=float(rec.get("limit_price") or 0),
            estimated_amount_krw=float(rec.get("estimated_amount_krw") or 0),
            adapter_status=policy.get("adapter_status", "disabled"),
            live_order_allowed=bool(policy.get("live_order_allowed")),
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
