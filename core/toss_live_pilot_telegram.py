"""core/toss_live_pilot_telegram.py

승인형 Live Pilot Telegram UX — 메시지 포맷 + InlineKeyboard + callback handler.

이번 단계에서도 실제 주문 API 호출 절대 없음.
confirm 버튼을 눌러도 adapter disabled로 차단됨.

callback prefix: tlp: (Paper tp: 와 완전 분리)
형식: tlp:<action>:<preview_id>

금지:
- 실제 Toss 주문 API HTTP 쓰기 호출 (POST/PUT/DELETE/PATCH)
- live_order_sent=True 반환
- accountNo/token/key/secret 출력
- live_order_allowed=True 반환
- 금지 CTA: 매수하기/매도하기/주문 실행/자동매매 시작/실주문: 활성
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ─── callback prefix (Paper tp: 와 분리) ─────────────────
CB_PREFIX = "tlp:"


def build_callback_data(action: str, preview_id: str) -> str:
    """Telegram callback data 문자열 생성. 민감정보 미포함.

    action: review | confirm | cancel
    """
    return f"{CB_PREFIX}{action}:{preview_id}"


def parse_callback_data(data: str) -> dict | None:
    """callback data 파싱. 잘못된 형식이면 None."""
    if not data or not data.startswith(CB_PREFIX):
        return None
    parts = data[len(CB_PREFIX):].split(":", 1)
    if len(parts) < 2:
        return None
    return {
        "action": parts[0],
        "preview_id": parts[1],
    }


# ─── 메시지 포맷 ─────────────────────────────────────────

def format_live_pilot_preview_message(
    preview: dict,
    payload_result: dict,
    policy: dict,
) -> str:
    """Live Pilot 미리보기 Telegram 메시지 생성.

    금지 CTA 없음 — 매수하기/매도하기/주문 실행/실주문: 활성 절대 미포함.
    """
    from core.toss_live_pilot_hermes_bridge import get_symbol_display
    symbol = preview.get("symbol", "")
    symbol_display = get_symbol_display(symbol)
    side = preview.get("side", "buy")
    side_label = "매수 후보" if side == "buy" else "매도 후보"
    qty = preview.get("quantity", 0)
    price = preview.get("limit_price", 0)
    amount = preview.get("estimated_amount_krw", 0)
    max_krw = policy.get("max_order_krw", 100_000)
    max_daily = policy.get("max_daily_krw", 300_000)
    max_per_day = policy.get("max_orders_per_day", 1)
    blocks = preview.get("blocks", [])
    ok = preview.get("ok", False) and not blocks

    lines = [
        "[승인형 Live Pilot 미리보기]",
        "아직 주문 전송 안 함",
        "실주문: 비활성",
        "최종 2단계 승인 필요",
        "주문 API 호출 비활성",
        "",
    ]

    if not ok:
        lines.append(f"차단: {symbol_display}")
        for b in blocks:
            lines.append(f"  · {b}")
        lines.append("dry-run payload 생성 불가")
    else:
        lines.append(f"{symbol_display}")
        lines.append(f"- 방향: {side_label}")
        lines.append("- 주문유형: 지정가")
        lines.append(f"- 지정가: ₩{price:,.0f}")
        lines.append(f"- 수량: {qty}주")
        lines.append(f"- 예상금액: ₩{amount:,.0f}")
        lines.append(f"- 1회 한도: ₩{max_krw:,.0f}")
        lines.append(f"- 일일 한도: ₩{max_daily:,.0f}")
        lines.append(f"- 일일 최대 주문: {max_per_day}건")
        lines.append(f"- adapter: {policy.get('adapter_status', 'disabled')}")
        lines.append("")
        lines.append("다음 단계: 최종 승인 버튼을 눌러도 이번 단계에서는 전송 차단됩니다.")

    return "\n".join(lines)


# ─── InlineKeyboard 생성 ─────────────────────────────────

def build_live_pilot_keyboard(
    preview_id: str,
    preview: dict,
) -> list[list[dict]]:
    """Live Pilot Telegram InlineKeyboard 생성.

    callback prefix: tlp: (Paper tp: 와 완전 분리)
    """
    ok = preview.get("ok", False) and not preview.get("blocks")

    if not ok:
        return [[
            {"text": "취소", "callback_data": build_callback_data("cancel", preview_id)},
        ]]

    return [
        [
            {"text": "Live Pilot 검토 완료", "callback_data": build_callback_data("review", preview_id)},
        ],
        [
            {"text": "최종 승인 시도(차단됨)", "callback_data": build_callback_data("confirm", preview_id)},
            {"text": "취소", "callback_data": build_callback_data("cancel", preview_id)},
        ],
    ]


# ─── callback handler ────────────────────────────────────

def handle_live_pilot_callback(callback_data: str) -> dict:
    """Telegram callback 처리.

    confirm을 눌러도 adapter disabled로 차단됨.
    실제 주문 API 호출 없음.

    반환: {ok, action, message, live_order_sent}
    """
    parsed = parse_callback_data(callback_data)
    if not parsed:
        return {
            "ok": False,
            "action": "unknown",
            "live_order_sent": False,
            "message": "잘못된 요청입니다.\n실주문: 비활성\n아직 주문 전송 안 함",
        }

    action = parsed["action"]
    preview_id = parsed["preview_id"]

    if action == "review":
        return _handle_review(preview_id)
    elif action == "confirm":
        return _handle_confirm(preview_id)
    elif action == "cancel":
        return _handle_cancel(preview_id)
    else:
        return {
            "ok": False,
            "action": action,
            "live_order_sent": False,
            "message": f"알 수 없는 액션: {action}\n실주문: 비활성",
        }


def _handle_review(preview_id: str) -> dict:
    """검토 완료 — preview 상태 갱신, 주문 없음."""
    try:
        from core.toss_live_pilot_ledger import record_reviewed
        result = record_reviewed(preview_id)
        ok = result.get("ok", False)
        msg = (
            "[Live Pilot 검토 완료]\n"
            "아직 주문 전송 안 함\n"
            "실주문: 비활성\n"
            "주문 API 호출 비활성\n"
            f"pilot_id: {result.get('pilot_id', preview_id)}\n"
            f"상태: {result.get('status', 'reviewed')}"
        )
    except Exception as e:
        logger.warning("live pilot review failed: %s", e)
        ok = False
        msg = f"검토 기록 오류: {e}\n실주문: 비활성"

    # 이벤트 기록
    try:
        from core.toss_live_pilot_events import record_event
        record_event(
            pilot_id=preview_id,
            event_type="reviewed",
            status="reviewed",
            preview_id=preview_id,
            message="Live Pilot 검토 완료",
        )
    except Exception as e:
        logger.warning("review event record failed: %s", e)

    return {"ok": ok, "action": "review", "live_order_sent": False, "message": msg}


def _handle_confirm(preview_id: str) -> dict:
    """최종 승인 시도.

    순서:
      1. pilot record 조회
      2. Hermes verification PASS 확인 (gate)
      3. policy/adapter 확인
      4. can_send guards
      5. transport dispatch (transport=None 기본 → blocked)
    """
    from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
    from core.toss_live_pilot_adapter import (
        can_send_live_pilot_order,
        dispatch_toss_order_live,
    )
    from core.toss_live_pilot_ledger import (
        record_confirm_attempt,
        record_live_send_blocked,
        record_live_sent,
        record_live_send_failed,
        list_live_pilot_records,
    )
    from core.toss_live_pilot_verification import is_verification_passed

    # 1. pilot record 조회
    records = list_live_pilot_records(limit=200)
    matched = [r for r in records if r.get("pilot_id") == preview_id]
    if not matched:
        return {
            "ok": False, "action": "confirm", "live_order_sent": False, "blocked": True,
            "reason": "pilot_id_not_found",
            "message": "pilot_id를 찾을 수 없습니다.\n아직 주문 전송 안 함",
        }
    rec = matched[0]

    # 공통 이벤트 필드 추출 헬퍼
    def _event_fields(r: dict) -> dict:
        sym = r.get("symbol", "")
        return dict(
            symbol=sym,
            side=r.get("side", "buy"),
            quantity=int(r.get("quantity") or 0),
            limit_price=float(r.get("limit_price") or 0),
            estimated_amount_krw=float(r.get("estimated_amount_krw") or 0),
            adapter_status=r.get("adapter_status", "disabled"),
        )

    # 2. Hermes verification gate
    verif_ok, verif_reasons, verif_rec = is_verification_passed(preview_id)
    verif_status = verif_rec.get("status", "PENDING") if verif_rec else "NOT_FOUND"
    verification_id = verif_rec.get("verification_id", "") if verif_rec else ""

    if not verif_ok:
        try:
            record_live_send_blocked(preview_id, verif_reasons)
        except Exception as e:
            logger.warning("hermes gate ledger failed: %s", e)
        msg = (
            "[차단: Hermes 교차검증 미완료/미통과]\n"
            "아직 주문 전송 안 함\n"
            "live_order_sent=false\n"
            f"Hermes 검증 상태: {verif_status}\n"
            f"차단 사유: {'; '.join(verif_reasons[:2])}"
        )
        try:
            from core.toss_live_pilot_events import record_event
            record_event(
                pilot_id=preview_id, event_type="confirm_blocked_hermes",
                status="live_send_blocked", preview_id=preview_id,
                verification_id=verification_id,
                reason="hermes_verification_required", message=msg,
                **_event_fields(rec),
            )
        except Exception as e:
            logger.warning("confirm_blocked_hermes event failed: %s", e)
        return {
            "ok": False, "action": "confirm", "live_order_sent": False, "blocked": True,
            "reason": "hermes_verification_required",
            "verif_status": verif_status,
            "verif_reasons": verif_reasons,
            "message": msg,
        }

    # 3. policy/adapter 확인
    policy = compute_toss_live_pilot_policy()
    if not policy.get("live_order_allowed") or policy.get("adapter_status") != "enabled":
        try:
            record_confirm_attempt(preview_id)
        except Exception as e:
            logger.warning("confirm ledger failed: %s", e)
        msg = (
            "[Hermes 검증 PASS 확인]\n"
            "차단: live pilot 조건 미충족\n"
            "아직 주문 전송 안 함\n"
            "live_order_sent=false\n"
            "실주문: 비활성"
        )
        try:
            from core.toss_live_pilot_events import record_event
            record_event(
                pilot_id=preview_id, event_type="confirm_blocked_policy",
                status="confirmed_but_not_sent", preview_id=preview_id,
                verification_id=verification_id,
                adapter_status=policy.get("adapter_status", "disabled"),
                reason="live_pilot_conditions_not_met", message=msg,
                **{k: v for k, v in _event_fields(rec).items() if k != "adapter_status"},
            )
        except Exception as e:
            logger.warning("confirm_blocked_policy event failed: %s", e)
        return {
            "ok": False, "action": "confirm", "live_order_sent": False, "blocked": True,
            "reason": "toss_order_adapter_disabled",
            "adapter_status": policy.get("adapter_status", "disabled"),
            "message": msg,
        }

    # 4. can_send guards
    preview_stub = {
        "ok": rec.get("status") not in ("blocked", "cancelled"),
        "symbol": rec.get("symbol", ""),
        "side": rec.get("side", "buy"),
        "quantity": rec.get("quantity", 0),
        "limit_price": rec.get("limit_price", 0),
        "estimated_amount_krw": rec.get("estimated_amount_krw", 0),
        "blocks": rec.get("blocks", []),
        "live_order_sent": bool(rec.get("live_order_sent")),
    }
    payload_result_stub = {"ok": preview_stub["ok"], "live_order_sent": preview_stub["live_order_sent"]}

    can_send, guard_reasons = can_send_live_pilot_order(policy, preview_stub, payload_result_stub)
    if not can_send:
        try:
            record_live_send_blocked(preview_id, guard_reasons)
        except Exception as e:
            logger.warning("live_send_blocked ledger failed: %s", e)

        # sell 차단 여부 확인 → 전용 문구
        if any("sell_not_allowed" in r for r in guard_reasons):
            guard_msg = (
                "차단: BUY_ONLY pilot — 매도는 아직 비활성\n"
                "아직 주문 전송 안 함\n"
                "live_order_sent=false"
            )
        else:
            guard_msg = (
                "[차단: 한도/중복/가격 조건 미충족]\n"
                "주문 전송 안 함\n"
                "live_order_sent=false\n"
                f"차단 사유: {'; '.join(guard_reasons[:3])}"
            )
        try:
            from core.toss_live_pilot_events import record_event
            record_event(
                pilot_id=preview_id, event_type="live_send_blocked",
                status="live_send_blocked", preview_id=preview_id,
                verification_id=verification_id,
                reason="live_send_guard_failed", message=guard_msg,
                **_event_fields(rec),
            )
        except Exception as e:
            logger.warning("live_send_blocked event failed: %s", e)
        return {
            "ok": False, "action": "confirm", "live_order_sent": False, "blocked": True,
            "reason": "live_send_guard_failed",
            "guard_reasons": guard_reasons,
            "message": guard_msg,
        }

    # 5. transport dispatch (transport=None → blocked)
    payload = {
        "symbol": preview_stub["symbol"],
        "side": preview_stub["side"],
        "order_type": "limit",
        "quantity": preview_stub["quantity"],
        "limit_price": preview_stub["limit_price"],
        "estimated_amount_krw": preview_stub["estimated_amount_krw"],
    }
    dispatch_result = dispatch_toss_order_live(payload, policy, transport=None)

    if dispatch_result.get("live_order_sent"):
        try:
            record_live_sent(
                preview_id,
                broker_order_id=dispatch_result.get("broker_order_id", ""),
                payload_hash=dispatch_result.get("payload_hash", ""),
            )
        except Exception as e:
            logger.warning("live_sent ledger failed: %s", e)
        sent_msg = dispatch_result.get("message", "승인형 live pilot 주문 전송 완료")
        try:
            from core.toss_live_pilot_events import record_event
            record_event(
                pilot_id=preview_id, event_type="live_sent",
                status="live_sent", preview_id=preview_id,
                verification_id=verification_id,
                live_order_sent=True, message=sent_msg,
                **_event_fields(rec),
            )
        except Exception as e:
            logger.warning("live_sent event failed: %s", e)
        return {
            "ok": True, "action": "confirm",
            "live_order_sent": True,
            "message": sent_msg,
        }
    else:
        try:
            record_live_send_failed(
                preview_id,
                failure_reason=dispatch_result.get("reason", ""),
                payload_hash=dispatch_result.get("payload_hash", ""),
            )
        except Exception as e:
            logger.warning("live_send_failed ledger failed: %s", e)

        dispatch_reason = dispatch_result.get("reason", "transport_blocked")
        _transport_not_configured_reasons = frozenset([
            "live_transport_not_injected",
            "toss_live_transport_not_configured",
        ])
        if dispatch_reason in _transport_not_configured_reasons:
            dispatch_msg = (
                "[Hermes 검증 PASS 확인]\n"
                "차단: Toss live transport 미설정\n"
                "아직 주문 전송 안 함\n"
                "live_order_sent=false"
            )
            dispatch_event_type = "confirm_blocked_transport"
        else:
            dispatch_msg = dispatch_result.get(
                "message",
                "주문 전송 조건 미충족\n아직 주문 전송 안 함\nlive_order_sent=false",
            )
            dispatch_event_type = "live_send_failed"
        try:
            from core.toss_live_pilot_events import record_event
            record_event(
                pilot_id=preview_id, event_type=dispatch_event_type,
                status="live_send_blocked", preview_id=preview_id,
                verification_id=verification_id,
                reason=dispatch_reason, message=dispatch_msg,
                **_event_fields(rec),
            )
        except Exception as e:
            logger.warning("%s event failed: %s", dispatch_event_type, e)
        return {
            "ok": False, "action": "confirm", "live_order_sent": False, "blocked": True,
            "reason": dispatch_reason,
            "message": dispatch_msg,
        }


def _handle_cancel(preview_id: str) -> dict:
    """취소 처리 — live pilot ledger status=cancelled."""
    try:
        from core.toss_live_pilot_ledger import cancel_live_pilot
        result = cancel_live_pilot(preview_id, reason="user_cancelled_via_telegram")
        ok = result.get("ok", False)
        msg = (
            "[Live Pilot 취소]\n"
            "아직 주문 전송 안 함\n"
            "실주문: 비활성\n"
            f"상태: {result.get('status', 'cancelled')}"
        )
    except Exception as e:
        logger.warning("live pilot cancel failed: %s", e)
        ok = False
        msg = f"취소 기록 오류: {e}\n실주문: 비활성"

    # 이벤트 기록
    try:
        from core.toss_live_pilot_events import record_event
        record_event(
            pilot_id=preview_id,
            event_type="cancelled",
            status="cancelled",
            preview_id=preview_id,
            message="Live Pilot 취소",
        )
    except Exception as e:
        logger.warning("cancel event record failed: %s", e)

    return {"ok": ok, "action": "cancel", "live_order_sent": False, "message": msg}


# ─── Telegram 발송 ────────────────────────────────────────

def send_live_pilot_preview_message(
    text: str,
    inline_keyboard: list[list[dict]],
) -> bool:
    """InlineKeyboard가 달린 live pilot 미리보기 Telegram 발송.

    실제 주문 없음. 민감정보 미포함.
    """
    import json
    import requests as req

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram 미설정 — live pilot 발송 불가")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
        "reply_markup": json.dumps({"inline_keyboard": inline_keyboard}),
    }
    try:
        res = req.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            logger.info("Live Pilot 미리보기 발송 완료")
            return True
        logger.warning("Live Pilot 발송 실패: %d %s", res.status_code, res.text[:160])
        return False
    except Exception as e:
        logger.error("Live Pilot 발송 오류: %s", e)
        return False
