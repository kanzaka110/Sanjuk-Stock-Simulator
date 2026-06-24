"""core/toss_live_transport.py

Toss live order transport 인터페이스 + 기본 구현.

현재 상태: not_configured
- Toss 실제 주문 endpoint 미확인 (read-only API만 확인됨)
- 실제 HTTP transport는 endpoint/필드/응답 스키마 확인 후 별도 구현
- 기본 구현체(NotConfiguredTossLiveTransport)는 항상 blocked 반환

인터페이스:
  transport.send_buy_order(payload: dict) → dict

금지:
  - 추측 endpoint HTTP POST 금지
  - accountNo/token/key/secret payload 포함 금지
  - sell 주문 구현 금지 (BUY_ONLY)
  - live_order_sent=True를 endpoint 확인 없이 반환 금지
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 전역 transport 상태 — endpoint 확인 전까지 not_configured 유지
LIVE_TRANSPORT_STATUS: str = "not_configured"


class TossLiveTransportBase:
    """Toss live transport 기본 인터페이스."""

    def send_buy_order(self, payload: dict) -> dict:
        """매수 주문 전송. 서브클래스에서 구현.

        Args:
            payload: 민감정보 제외된 주문 payload
                (symbol, side, order_type, quantity, limit_price, estimated_amount_krw)

        Returns:
            {"ok": bool, "live_order_sent": bool, "reason": str, ...}

        금지:
            - accountNo/token/key/secret 포함 금지
            - sell 주문 구현 금지
        """
        raise NotImplementedError


class NotConfiguredTossLiveTransport(TossLiveTransportBase):
    """Toss live transport 미설정 상태 구현체.

    endpoint 확인 전까지 사용. 항상 not_configured blocked 반환.
    """

    def send_buy_order(self, payload: dict) -> dict:
        """매수 주문 전송 — endpoint 미설정으로 항상 차단."""
        symbol = payload.get("symbol", "unknown")
        log.info(
            "live transport not configured: symbol=%s — endpoint 확인 필요",
            symbol,
        )
        return {
            "ok": False,
            "blocked": True,
            "reason": "toss_live_transport_not_configured",
            "live_order_sent": False,
            "transport_status": LIVE_TRANSPORT_STATUS,
            "message": (
                "차단: Toss live transport 미설정\n"
                "아직 주문 전송 안 함\n"
                "live_order_sent=false\n"
                "Toss 주문 endpoint 확인 후 재설정 필요"
            ),
        }


# 기본 transport 인스턴스 (항상 not_configured)
DEFAULT_LIVE_TRANSPORT = NotConfiguredTossLiveTransport()


def get_transport_status() -> dict:
    """현재 transport 설정 상태 반환 (read-only)."""
    return {
        "status": LIVE_TRANSPORT_STATUS,
        "live_order_sent_possible": False,
        "endpoint_confirmed": False,
        "description": "Toss 주문 endpoint 미확인 — 실제 transport 미설정",
    }
