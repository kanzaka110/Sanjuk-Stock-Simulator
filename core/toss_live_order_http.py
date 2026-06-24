"""core/toss_live_order_http.py

Toss 주문 생성 HTTP 위임 모듈 (LiveTossTransport 전용).

민감 header/secret 처리를 이 모듈에 격리한다.
- token/account는 core.toss_client의 기존 로더만 재사용 (새 secret loader 없음)
- 실제 POST는 명시적 호출 시에만 발생 (production 기본 경로 자동 호출 없음)
- 반환값/로그에 token/account/header 노출 금지 (값 마스킹)

[중요]
- 이 모듈을 import한다고 주문이 전송되지 않는다.
- submit_buy_order()를 호출해야만 전송되며, 호출부(LiveTossTransport)는
  명시적 transport 주입 + 다단계 guard 통과 시에만 실행된다.
- 기본 transport는 NotConfigured이고 env gate 3개는 꺼져 있다.
"""

from __future__ import annotations

import logging
import re

from core import toss_client as tc

log = logging.getLogger(__name__)

# 주문 생성 endpoint (공식 확인됨)
_ORDER_PATH = "/api/v1/orders"

# 요청 header 키 (값 아님 — 비밀 아님)
_H_AUTH = "Authorization"
_H_ACCOUNT = "X-Tossinvest-Account"
_H_CT = "Content-Type"
_AUTH_SCHEME = "Bearer"

# broker order id 마스킹 패턴
_ACCOUNT_RE = re.compile(r"\d{8}-\d{2}")


def _mask(value) -> str:
    """민감 패턴(계좌형식/긴 숫자) 마스킹."""
    s = str(value or "")
    s = _ACCOUNT_RE.sub("[masked]", s)
    s = tc._LONG_NUM_RE.sub("[NUM_REDACTED]", s)
    return s


def _resolve_account_seq(account_seq: str | None) -> str | None:
    """accountSeq 결정 — 미지정 시 기존 toss_client 계좌 조회 재사용."""
    if account_seq:
        return str(account_seq)
    try:
        accounts = tc.get_accounts()
    except Exception as e:
        log.warning("account 조회 실패: %s", str(e)[:80])
        return None
    if not accounts:
        return None
    seq = str(accounts[0].get("accountSeq", ""))
    return seq or None


def submit_buy_order(
    request_body: dict,
    *,
    account_seq: str | None = None,
    timeout: float | None = None,
) -> dict:
    """검증 완료된 주문 request body를 실제 Toss endpoint로 전송.

    [전제] request_body는 build_toss_order_create_request() 결과의 'request'.
    민감정보(accountNo/token/key/secret) 미포함 상태여야 한다.

    Returns:
        {"ok", "blocked"|"failed", "live_order_sent", "reason",
         "transport_status", "broker_order_id"(masked), "message"}
        — token/account/header 등 민감정보 미포함.
    """
    import requests

    # 1. token (기존 toss_client 로더 재사용)
    token = tc._get_access_token()
    if not token:
        log.info("live order blocked: token unavailable")
        return {
            "ok": False,
            "blocked": True,
            "live_order_sent": False,
            "reason": "token_unavailable",
            "transport_status": "live_send_blocked",
            "message": "차단: 인증 토큰 없음 — 아직 주문 전송 안 함\nlive_order_sent=false",
        }

    # 2. accountSeq
    seq = _resolve_account_seq(account_seq)
    if not seq:
        log.info("live order blocked: account unavailable")
        return {
            "ok": False,
            "blocked": True,
            "live_order_sent": False,
            "reason": "account_unavailable",
            "transport_status": "live_send_blocked",
            "message": "차단: 계좌 정보 없음 — 아직 주문 전송 안 함\nlive_order_sent=false",
        }

    # 3. headers (값은 반환/로그에 미포함)
    headers = {
        _H_AUTH: f"{_AUTH_SCHEME} {token}",
        _H_ACCOUNT: seq,
        _H_CT: "application/json",
    }

    base = tc.TOSS_BASE_URL
    url = f"{base}{_ORDER_PATH}"
    to = timeout if timeout is not None else tc.TIMEOUT

    # 4. 실제 전송 (network/HTTP error는 안전 반환)
    try:
        resp = requests.post(url, headers=headers, json=request_body, timeout=to)
    except requests.RequestException as e:
        log.warning("live order network error: %s", str(e)[:80])
        return {
            "ok": False,
            "failed": True,
            "live_order_sent": False,
            "reason": "network_error",
            "transport_status": "live_send_failed",
            "message": "주문 전송 실패: network error\n주문 전송 비활성\nlive_order_sent=false",
        }

    if resp.status_code not in (200, 201):
        log.warning("live order http error: status=%d", resp.status_code)
        return {
            "ok": False,
            "failed": True,
            "live_order_sent": False,
            "reason": f"http_{resp.status_code}",
            "transport_status": "live_send_failed",
            "message": (
                f"주문 전송 실패: HTTP {resp.status_code}\n"
                "주문 전송 비활성\nlive_order_sent=false"
            ),
        }

    # 5. 성공 — broker order id 마스킹 후 반환
    try:
        body = resp.json()
    except Exception:
        body = {}
    result = body.get("result", {}) if isinstance(body, dict) else {}
    raw_order_id = ""
    if isinstance(result, dict):
        raw_order_id = result.get("orderId") or result.get("orderNo") or ""

    log.info("live order sent: status=%d", resp.status_code)
    return {
        "ok": True,
        "live_order_sent": True,
        "reason": "live_sent",
        "transport_status": "live_sent",
        "broker_order_id": _mask(raw_order_id),
        "message": (
            "승인형 매수 pilot 전송 완료\n"
            "자동매매 아님 — Hermes PASS + 사용자 최종 승인 1건\n"
            "live_order_sent=true"
        ),
    }
