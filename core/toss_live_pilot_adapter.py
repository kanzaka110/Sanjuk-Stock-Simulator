"""core/toss_live_pilot_adapter.py

Toss Live Pilot 주문 어댑터 stub + payload 생성/검증.

이번 단계에서는 실제 주문 API를 절대 호출하지 않는다.
모든 dispatch 시도는 blocked 반환.
payload 생성은 가능하나 민감정보(accountNo/token/key) 포함 금지.

금지:
- 실제 Toss 주문 API HTTP 쓰기 호출 (POST/PUT/DELETE/PATCH)
- live_order_sent=True 반환
- accountNo/token/key/secret payload 포함
- live_order_allowed=True 반환
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_ADAPTER_STATUS = "disabled"

_VALID_SIDES = frozenset(["buy", "sell"])
_VALID_ORDER_TYPES = frozenset(["limit"])


# ─── payload 생성 ─────────────────────────────────────────────────

def build_toss_order_payload(
    preview: dict,
    policy: dict | None = None,
) -> dict:
    """주문 요청 payload 생성 (dry-run only, HTTP 호출 없음).

    민감정보(accountNo/token/key/secret) 포함하지 않음.
    live_order_allowed는 항상 False.

    Returns:
        {"ok": bool, "dry_run": True, "payload": dict, "blocks": list, "warnings": list}
    """
    if policy is None:
        try:
            from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
            policy = compute_toss_live_pilot_policy()
        except Exception:
            policy = {"max_order_krw": 100_000, "blocked_symbols": ["161510.KS", "005930.KS"]}

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


# ─── dispatch stub (항상 차단) ────────────────────────────────────

def dispatch_toss_order_disabled(
    payload: dict,
    policy: dict | None = None,
) -> dict:
    """주문 dispatch stub — 항상 blocked 반환.

    env TOSS_LIVE_PILOT_ENABLED=true 여도 이번 단계에서는 전송 안 함.
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
        "description": "승인형 live pilot 준비 단계 — 실제 주문 API 연결 비활성",
    }
