"""core/toss_live_pilot_adapter.py

Toss Live Pilot 주문 어댑터 — payload 생성/검증 + 수동 승인 전용 dispatch.

[구조]
- build_toss_order_payload()   : dry-run payload 생성 (HTTP 호출 없음)
- dispatch_toss_order_disabled(): 항상 blocked (기존 stub 유지)
- can_send_live_pilot_order()  : 다단계 guard 검사
- dispatch_toss_order_live()   : transport 주입 시에만 전송 가능
  → transport=None (기본값)이면 절대 전송 안 함
  → 실제 Toss 주문 API endpoint가 미확인이므로 live_transport도 stub 수준 유지

[활성화 조건]
3개 env gate 모두 true + can_send_live_pilot_order 통과 + transport 명시 주입 시에만 전송.
기본 실행/테스트에서는 실제 주문 0건.

금지:
- 실제 Toss 주문 API HTTP 쓰기 호출 (POST/PUT/DELETE/PATCH) 자동 실행 금지
- live_order_sent=True를 guard 없이 반환 금지
- accountNo/token/key/secret payload 포함 금지
- transport=None 상태에서 HTTP 호출 금지
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

_ADAPTER_STATUS = "disabled"   # 코드 기본값 (env gate로 override 가능)

_VALID_SIDES = frozenset(["buy", "sell"])
_VALID_ORDER_TYPES = frozenset(["limit"])

KST = timezone(timedelta(hours=9))


# ─── payload 생성 ─────────────────────────────────────────────────

def build_toss_order_payload(
    preview: dict,
    policy: dict | None = None,
) -> dict:
    """주문 요청 payload 생성 (dry-run only, HTTP 호출 없음).

    민감정보(accountNo/token/key/secret) 포함하지 않음.

    Returns:
        {"ok": bool, "dry_run": True, "payload": dict, "blocks": list, "warnings": list}
    """
    if policy is None:
        try:
            from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
            policy = compute_toss_live_pilot_policy()
        except Exception:
            policy = {"max_order_krw": 100_000, "blocked_symbols": []}

    symbol = preview.get("symbol", "")
    side = preview.get("side", "buy")
    quantity = int(preview.get("quantity") or 0)
    limit_price = float(preview.get("limit_price") or 0)
    estimated_krw = float(preview.get("estimated_amount_krw") or limit_price * quantity)
    currency = preview.get("currency", "KRW") or "KRW"

    blocks: list[str] = []
    warnings: list[str] = ["dry-run payload only", "주문 API 호출 비활성", "아직 주문 전송 안 함"]

    # preview 자체가 이미 차단됐으면 payload도 차단
    if preview.get("blocks"):
        blocks += list(preview["blocks"])

    # 추가 검증
    if symbol in (policy.get("blocked_symbols") or []):
        if "blocked_symbol" not in " ".join(blocks) and symbol not in " ".join(blocks):
            blocks.append(f"blocked_symbol: {symbol}")

    if side not in _VALID_SIDES:
        blocks.append(f"invalid_side: {side!r} (허용: buy/sell)")

    if quantity <= 0:
        blocks.append(f"invalid_quantity: {quantity} (>0 필요)")

    if limit_price <= 0:
        blocks.append("invalid_price: limit_price > 0 필요")

    max_krw = policy.get("max_order_krw", 100_000)
    if estimated_krw > max_krw and not blocks:
        blocks.append(f"금액_한도_초과: {estimated_krw:,.0f}원 > {max_krw:,.0f}원")

    ok = len(blocks) == 0

    payload: dict = {
        "symbol": symbol,
        "side": side,
        "order_type": "limit",
        "quantity": quantity,
        "limit_price": limit_price,
        "currency": currency,
        "estimated_amount_krw": estimated_krw,
        # 민감정보 필드 없음: accountNo/token/key/secret 포함 금지
    }

    return {
        "ok": ok,
        "dry_run": True,
        "adapter_status": _ADAPTER_STATUS,
        "live_order_allowed": False,
        "live_order_sent": False,
        "payload": payload if ok else {},
        "blocks": blocks,
        "warnings": warnings,
    }


# ─── dispatch stub (항상 차단, 하위 호환) ─────────────────────────

def dispatch_toss_order_disabled(
    payload: dict,
    policy: dict | None = None,
) -> dict:
    """주문 dispatch stub — 항상 blocked 반환.

    어떤 조건에서도 실제 API를 호출하지 않는다.
    """
    symbol = payload.get("symbol", "unknown")
    log.info("dispatch blocked (adapter disabled): symbol=%s", symbol)
    return {
        "ok": False,
        "blocked": True,
        "reason": "toss_order_adapter_disabled",
        "live_order_sent": False,
        "adapter_status": _ADAPTER_STATUS,
        "live_order_allowed": False,
        "symbol": symbol,
        "message": "아직 주문 전송 안 함 — adapter disabled",
    }


# ─── 다단계 guard 검사 ────────────────────────────────────────────

def can_send_live_pilot_order(
    policy: dict,
    preview: dict,
    payload_result: dict,
) -> tuple[bool, list[str]]:
    """실제 주문 전송 가능 여부를 다단계로 검사.

    Returns:
        (ok: bool, reasons: list[str])  — ok=True면 전송 조건 충족.
    """
    reasons: list[str] = []

    # 1. policy gate
    if not policy.get("live_pilot_enabled"):
        reasons.append("live_pilot_enabled=false")
    if not policy.get("live_order_allowed"):
        reasons.append("live_order_allowed=false")
    if policy.get("adapter_status") != "enabled":
        reasons.append(f"adapter_status={policy.get('adapter_status', 'disabled')}")
    if not policy.get("requires_user_confirmation"):
        reasons.append("requires_user_confirmation missing")
    if not policy.get("requires_second_confirmation"):
        reasons.append("requires_second_confirmation missing")

    # 1.5 BUY_ONLY side guard (policy gate 다음, 나머지 guard 전에 체크)
    side = preview.get("side", "")
    allowed_sides = policy.get("allowed_sides", ["buy"])
    if side not in allowed_sides:
        reasons.append(f"sell_not_allowed_in_buy_only_pilot: side={side!r}")

    # 2. preview valid
    if not preview.get("ok"):
        reasons.append("preview_not_ok")
    if preview.get("blocks"):
        reasons.append(f"preview_blocked: {preview['blocks']}")
    if preview.get("live_order_sent"):
        reasons.append("preview live_order_sent=true (duplicate guard)")

    # 3. payload valid
    if not payload_result.get("ok"):
        reasons.append("payload_not_ok")
    if payload_result.get("live_order_sent"):
        reasons.append("payload live_order_sent=true")

    # 4. symbol guard
    symbol = preview.get("symbol", "")
    blocked_symbols = set(policy.get("blocked_symbols", []))
    if symbol in blocked_symbols:
        reasons.append(f"blocked_symbol: {symbol}")

    # 5. amount guard
    estimated = float(preview.get("estimated_amount_krw") or 0)
    max_krw = policy.get("max_order_krw", 100_000)
    if estimated > max_krw:
        reasons.append(f"amount_over_limit: {estimated:,.0f} > {max_krw:,.0f}")

    # 6. price sanity
    limit_price = float(preview.get("limit_price") or 0)
    if limit_price <= 0:
        reasons.append("invalid_price")

    # 7. quantity sanity
    qty = int(preview.get("quantity") or 0)
    if qty <= 0:
        reasons.append("invalid_quantity")

    # 8. daily guard (ledger 조회)
    try:
        _daily_reasons = _check_daily_limits(symbol, estimated, policy)
        reasons.extend(_daily_reasons)
    except Exception as e:
        log.warning("daily guard 조회 실패: %s", e)
        reasons.append("daily_guard_check_failed")

    ok = len(reasons) == 0
    return ok, reasons


def _check_daily_limits(symbol: str, estimated_krw: float, policy: dict) -> list[str]:
    """당일 live order 한도/중복 체크 (ledger 조회)."""
    reasons: list[str] = []
    try:
        from core.toss_live_pilot_ledger import list_live_pilot_records
        today = datetime.now(KST).strftime("%Y-%m-%d")
        records = list_live_pilot_records(limit=100)

        today_sent = [
            r for r in records
            if r.get("status") == "live_sent"
            and r.get("created_at", "").startswith(today)
        ]

        max_orders = policy.get("max_orders_per_day", 1)
        if len(today_sent) >= max_orders:
            reasons.append(f"daily_order_count_exceeded: {len(today_sent)}/{max_orders}")

        max_daily = policy.get("max_daily_krw", 300_000)
        today_total = sum(float(r.get("estimated_amount_krw") or 0) for r in today_sent)
        if today_total + estimated_krw > max_daily:
            reasons.append(
                f"daily_amount_exceeded: {today_total + estimated_krw:,.0f} > {max_daily:,.0f}"
            )

        # 중복 symbol 체크
        today_sent_symbols = {r.get("symbol") for r in today_sent}
        if symbol in today_sent_symbols:
            reasons.append(f"duplicate_symbol_today: {symbol}")

    except Exception as e:
        reasons.append(f"daily_limit_check_error: {e}")

    return reasons


# ─── 실제 전송 함수 (transport 주입 필수) ────────────────────────

def dispatch_toss_order_live(
    payload: dict,
    policy: dict,
    *,
    transport=None,
) -> dict:
    """승인형 live pilot 주문 전송.

    [중요] transport=None(기본값)이면 절대 전송하지 않고 blocked 반환.
    실제 HTTP transport는 명시적으로 주입된 경우에만 사용.
    테스트에서는 fake transport만 사용.

    실제 Toss 주문 API endpoint가 미확인 상태이므로
    live_transport 구현체도 별도 주입이 필요하며 기본값으로 활성화되지 않음.

    민감정보(accountNo/token/key/secret) payload/반환값에 포함 금지.

    Args:
        payload: build_toss_order_payload() 결과의 payload 필드
        policy: compute_toss_live_pilot_policy() 결과
        transport: callable(payload, policy) → dict | None
                   None이면 항상 blocked.

    Returns:
        {"ok": bool, "live_order_sent": bool, "reason": str, ...}
    """
    symbol = payload.get("symbol", "unknown")

    # transport 없으면 항상 차단
    if transport is None:
        log.info("live dispatch blocked: transport not injected, symbol=%s", symbol)
        return {
            "ok": False,
            "blocked": True,
            "reason": "live_transport_not_injected",
            "live_order_sent": False,
            "adapter_status": policy.get("adapter_status", "disabled"),
            "live_order_allowed": policy.get("live_order_allowed", False),
            "symbol": symbol,
            "message": "주문 전송 조건 미충족 — transport not injected\n아직 주문 전송 안 함",
        }

    # payload 해시 (로그용, 민감정보 제외)
    safe_payload = {k: v for k, v in payload.items()
                    if k not in ("accountNo", "token", "key", "secret", "password")}
    payload_hash = hashlib.sha256(
        json.dumps(safe_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]

    log.info(
        "live pilot order attempt: symbol=%s qty=%s price=%s hash=%s",
        symbol, payload.get("quantity"), payload.get("limit_price"), payload_hash,
    )

    try:
        transport_result = transport(safe_payload, policy)
    except Exception as e:
        log.error("transport 호출 실패: %s", e)
        return {
            "ok": False,
            "blocked": False,
            "reason": "transport_exception",
            "live_order_sent": False,
            "failure_reason": str(e),
            "symbol": symbol,
            "payload_hash": payload_hash,
            "message": f"주문 전송 실패: {e}\n주문 전송 비활성",
        }

    # transport 성공 여부 판단 (transport 결과 신뢰)
    sent = bool(transport_result.get("ok")) and bool(transport_result.get("live_order_sent"))
    broker_order_id = transport_result.get("broker_order_id", "")
    # broker_order_id에서 민감 패턴 제거 (accountNo 형식 등)
    import re as _re
    broker_order_id = _re.sub(r'\d{8}-\d{2}', "[masked]", str(broker_order_id))

    result = {
        "ok": sent,
        "blocked": False,
        "live_order_sent": sent,
        "adapter_status": policy.get("adapter_status", "disabled"),
        "live_order_allowed": policy.get("live_order_allowed", False),
        "symbol": symbol,
        "quantity": payload.get("quantity"),
        "limit_price": payload.get("limit_price"),
        "estimated_amount_krw": payload.get("estimated_amount_krw"),
        "broker_order_id": broker_order_id,
        "payload_hash": payload_hash,
        "transport_status": transport_result.get("status", ""),
        "failure_reason": (
            transport_result.get("failure_reason")
            or transport_result.get("reason")
            or ""
        ) if not sent else "",
    }

    if sent:
        result["message"] = (
            "승인형 매수 pilot 전송 완료\n"
            "자동매매 아님\n"
            "Hermes PASS + 사용자 최종 승인 1건\n"
            "live_order_sent=true"
        )
        log.info("live pilot order sent: symbol=%s hash=%s", symbol, payload_hash)
    else:
        _fail_reason = (
            transport_result.get("failure_reason")
            or transport_result.get("reason")
            or "unknown"
        )
        result["message"] = (
            f"주문 전송 실패: {_fail_reason}\n"
            "주문 전송 비활성"
        )
        log.warning("live pilot order failed: symbol=%s reason=%s", symbol, result["failure_reason"])

    return result


# ─── 기존 stub (하위 호환) ────────────────────────────────────────

def send_live_pilot_order_stub(preview: dict) -> dict:
    """Live pilot 주문 전송 stub — 항상 blocked 반환 (하위 호환)."""
    symbol = preview.get("symbol", "unknown")
    preview_id = preview.get("preview_id", "unknown")
    log.info("live pilot order attempt blocked: preview_id=%s symbol=%s", preview_id, symbol)
    return {
        "ok": False,
        "blocked": True,
        "reason": "live_pilot_order_adapter_disabled",
        "live_order_sent": False,
        "adapter_status": _ADAPTER_STATUS,
        "preview_id": preview_id,
        "symbol": symbol,
        "message": "아직 주문 전송 안 함 — adapter disabled",
    }


def get_adapter_status() -> dict:
    """현재 adapter 상태 반환 (read-only)."""
    return {
        "status": _ADAPTER_STATUS,
        "live_order_allowed": False,
        "description": "승인형 live pilot — transport 주입 + 3개 env gate 통과 시에만 전송 가능",
    }
