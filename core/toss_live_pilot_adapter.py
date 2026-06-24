"""core/toss_live_pilot_adapter.py

Toss Live Pilot 주문 어댑터 stub.

이번 단계에서는 실제 주문 API를 절대 호출하지 않는다.
모든 시도는 blocked 반환.

금지:
- 실제 Toss 주문 API 호출
- live_order_sent=True 반환
- 민감정보 로깅
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_ADAPTER_STATUS = "disabled"


def send_live_pilot_order_stub(preview: dict) -> dict:
    """Live pilot 주문 전송 stub — 항상 blocked 반환.

    실제 주문 연결은 별도 승인 단계에서만 가능.
    이 함수는 어떤 상황에서도 실제 API를 호출하지 않는다.
    """
    symbol = preview.get("symbol", "unknown")
    preview_id = preview.get("preview_id", "unknown")
    log.info(
        "live pilot order attempt blocked: preview_id=%s symbol=%s",
        preview_id, symbol,
    )
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
