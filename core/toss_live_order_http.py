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
import os
import re
import time

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


def _as_float(value) -> float:
    try:
        if isinstance(value, dict):
            value = value.get("amount") or value.get("value") or value.get("quantity")
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _holding_symbol(item: dict) -> str:
    for key in ("symbol", "ticker", "stockCode", "code", "shortCode", "instrumentCode"):
        value = item.get(key)
        if value:
            return str(value).strip().upper()
    stock = item.get("stock") or item.get("instrument") or {}
    if isinstance(stock, dict):
        return _holding_symbol(stock)
    return ""


def _holding_sellable_quantity(item: dict) -> float:
    """Toss holdings payload variants에서 매도가능/보유 수량을 보수적으로 추출."""
    for key in (
        "sellableQuantity", "sellableQty", "availableQuantity", "availableQty",
        "orderableQuantity", "orderableQty", "tradableQuantity", "tradableQty",
        "quantity", "qty", "shares",
    ):
        if key in item:
            qty = _as_float(item.get(key))
            if qty > 0:
                return qty
    for nested_key in ("holding", "balance", "position"):
        nested = item.get(nested_key)
        if isinstance(nested, dict):
            qty = _holding_sellable_quantity(nested)
            if qty > 0:
                return qty
    return 0.0


def _iter_holding_items(holdings) -> list[dict]:
    if isinstance(holdings, list):
        return [x for x in holdings if isinstance(x, dict)]
    if not isinstance(holdings, dict):
        return []
    for key in ("items", "stocks", "positions", "holdings", "balances"):
        value = holdings.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _current_sellable_quantity(account_seq: str, symbol: str) -> float:
    data = tc.get_holdings(account_seq)
    target = str(symbol or "").strip().upper()
    for item in _iter_holding_items(data):
        if _holding_symbol(item) == target:
            return _holding_sellable_quantity(item)
    return 0.0


def _wait_for_sellable_position(account_seq: str, symbol: str, required_qty: float) -> dict:
    """매수 직후 SELL 422 방지를 위해 매도가능수량 반영을 짧게 polling."""
    wait_seconds = _as_float(os.environ.get("TOSS_SELL_HOLDING_POLL_SECONDS", "8"))
    interval = _as_float(os.environ.get("TOSS_SELL_HOLDING_POLL_INTERVAL", "1")) or 1.0
    wait_seconds = max(0.0, wait_seconds)
    interval = max(0.2, interval)
    deadline = time.monotonic() + wait_seconds
    attempts = 0
    last_qty = 0.0
    last_error = ""

    while True:
        attempts += 1
        try:
            last_qty = _current_sellable_quantity(account_seq, symbol)
            last_error = ""
            if last_qty >= required_qty:
                return {"ok": True, "attempts": attempts, "sellable_quantity": last_qty}
        except Exception as e:
            last_error = str(e)[:120]
            log.warning("sellable holding check failed: %s", last_error)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "ok": False,
                "attempts": attempts,
                "sellable_quantity": last_qty,
                "last_error": last_error,
            }
        time.sleep(min(interval, remaining))



def _sanitize_order_row(row) -> dict:
    """Broker order row -> safe confirmation summary."""
    if not isinstance(row, dict):
        return {}
    status = row.get("status") or row.get("orderStatus") or row.get("state") or ""
    order_id = row.get("orderId") or row.get("orderNo") or row.get("id") or ""
    execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
    filled_qty = (
        row.get("filledQuantity") or row.get("executedQuantity") or row.get("filledQty") or row.get("executedQty")
        or execution.get("filledQuantity") or execution.get("executedQuantity") or 0
    )
    avg_price = (
        row.get("averageFilledPrice") or row.get("filledPrice") or row.get("executedPrice") or row.get("avgPrice")
        or execution.get("averageFilledPrice") or execution.get("filledPrice") or row.get("price") or 0
    )
    return {
        "broker_order_id": _mask(order_id),
        "broker_order_status": str(status or ""),
        "filled_quantity": _as_float(filled_qty),
        "filled_price": _as_float(avg_price),
        "raw_status_present": bool(status),
        "symbol": str(row.get("symbol") or row.get("stockCode") or ""),
        "side": str(row.get("side") or ""),
        "quantity": _as_float(row.get("quantity") or row.get("qty") or 0),
        "ordered_at": str(row.get("orderedAt") or row.get("createdAt") or ""),
        "filled_at": str(execution.get("filledAt") or row.get("filledAt") or ""),
    }


def _safe_get(path: str, *, account_seq: str, params: dict | None = None) -> dict:
    """Toss GET helper for post-order confirmation. No secrets in return."""
    import requests
    token = tc._get_access_token()
    if not token or not account_seq:
        return {"ok": False, "reason": "token_or_account_unavailable"}
    headers = {_H_AUTH: f"{_AUTH_SCHEME} {token}", _H_ACCOUNT: str(account_seq), _H_CT: "application/json"}
    try:
        resp = requests.get(f"{tc.TOSS_BASE_URL}{path}", headers=headers, params=params or {}, timeout=tc.TIMEOUT)
    except requests.RequestException as e:
        return {"ok": False, "reason": "network_error", "message": _mask(str(e))[:160]}
    if resp.status_code != 200:
        return {"ok": False, "reason": f"http_{resp.status_code}", "message": _mask(getattr(resp, "text", ""))[:300]}
    try:
        body = resp.json()
    except Exception:
        body = {}
    return {"ok": True, "body": tc.sanitize_dict(body)}


def list_orders(status: str = "OPEN", *, account_seq: str | None = None) -> dict:
    """List Toss orders by status: OPEN or CLOSED. Safe read-only confirmation."""
    seq = _resolve_account_seq(account_seq)
    if not seq:
        return {"ok": False, "reason": "account_unavailable", "orders": []}
    res = _safe_get(_ORDER_PATH, account_seq=seq, params={"status": status})
    if not res.get("ok"):
        return {**res, "orders": []}
    body = res.get("body") or {}
    result = body.get("result", []) if isinstance(body, dict) else []
    if isinstance(result, dict):
        for key in ("items", "orders", "content"):
            if isinstance(result.get(key), list):
                result = result[key]
                break
    orders = result if isinstance(result, list) else []
    return {"ok": True, "status": status, "orders": [_sanitize_order_row(x) for x in orders if isinstance(x, dict)]}


def get_order(order_id: str, *, account_seq: str | None = None) -> dict:
    """Get a single Toss order by broker orderId. Safe read-only confirmation."""
    seq = _resolve_account_seq(account_seq)
    if not seq:
        return {"ok": False, "reason": "account_unavailable"}
    oid = str(order_id or "").strip()
    if not oid:
        return {"ok": False, "reason": "order_id_missing"}
    res = _safe_get(f"{_ORDER_PATH}/{oid}", account_seq=seq)
    if not res.get("ok"):
        return res
    body = res.get("body") or {}
    result = body.get("result", {}) if isinstance(body, dict) else {}
    if isinstance(result, list) and result:
        result = result[0]
    summary = _sanitize_order_row(result if isinstance(result, dict) else {})
    return {"ok": True, **summary}


def confirm_order_state(order_id: str, *, account_seq: str | None = None) -> dict:
    """Confirm broker acceptance/fill state after POST."""
    oid = str(order_id or "").strip()
    if not oid:
        return {"ok": False, "reason": "order_id_missing", "broker_confirmed": False}
    single = get_order(oid, account_seq=account_seq)
    if single.get("ok"):
        return {"broker_confirmed": True, "source": "single_order", **single}
    for st in ("OPEN", "CLOSED"):
        listed = list_orders(st, account_seq=account_seq)
        if listed.get("ok"):
            for row in listed.get("orders") or []:
                if row.get("broker_order_id") == _mask(oid):
                    return {"broker_confirmed": True, "source": f"orders_{st.lower()}", **row}
    return {"ok": False, "reason": single.get("reason", "order_not_found"), "broker_confirmed": False}


def submit_order(
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

    # 3. SELL 전 보유/매도가능수량 반영 확인
    side = str(request_body.get("side", "")).upper()
    symbol = str(request_body.get("symbol", "")).strip().upper()
    required_qty = _as_float(request_body.get("quantity"))
    if side == "SELL":
        ready = _wait_for_sellable_position(seq, symbol, required_qty)
        if not ready.get("ok"):
            sellable = float(ready.get("sellable_quantity", 0.0))
            return {
                "ok": False,
                "blocked": True,
                "live_order_sent": False,
                "reason": "sellable_position_not_ready",
                "transport_status": "live_send_blocked",
                "sellable_quantity": sellable,
                "attempts": ready.get("attempts", 0),
                "message": (
                    "차단: 매도가능수량 반영 대기 실패 — 주문 전송 안 함\n"
                    f"symbol={symbol} required={required_qty:g} sellable={sellable:g}\n"
                    "live_order_sent=false"
                ),
            }

    # 4. headers (값은 반환/로그에 미포함)
    headers = {
        _H_AUTH: f"{_AUTH_SCHEME} {token}",
        _H_ACCOUNT: seq,
        _H_CT: "application/json",
    }

    base = tc.TOSS_BASE_URL
    url = f"{base}{_ORDER_PATH}"
    to = timeout if timeout is not None else tc.TIMEOUT

    # 5. 실제 전송 (network/HTTP error는 안전 반환)
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
        safe_body = _mask(getattr(resp, "text", "") or "")[:500]
        return {
            "ok": False,
            "failed": True,
            "live_order_sent": False,
            "reason": f"http_{resp.status_code}",
            "transport_status": "live_send_failed",
            "error_body": safe_body,
            "message": (
                f"주문 전송 실패: HTTP {resp.status_code}\n"
                "주문 전송 비활성\nlive_order_sent=false"
            ),
        }

    # 6. 성공 — broker order id 마스킹 후 반환
    try:
        body = resp.json()
    except Exception:
        body = {}
    result = body.get("result", {}) if isinstance(body, dict) else {}
    raw_order_id = ""
    if isinstance(result, dict):
        raw_order_id = result.get("orderId") or result.get("orderNo") or ""

    confirmation = confirm_order_state(raw_order_id, account_seq=seq) if raw_order_id else {"broker_confirmed": False, "reason": "order_id_missing"}
    confirmed = bool(confirmation.get("broker_confirmed"))
    broker_status = confirmation.get("broker_order_status", "")
    filled_qty = confirmation.get("filled_quantity", 0.0)
    filled_price = confirmation.get("filled_price", 0.0)

    log.info("live order sent: status=%d confirmed=%s broker_status=%s", resp.status_code, confirmed, broker_status)
    return {
        "ok": True,
        "live_order_sent": True,
        "reason": "live_sent",
        "transport_status": "live_sent",
        "broker_order_id": _mask(raw_order_id),
        "broker_confirmed": confirmed,
        "broker_order_status": broker_status,
        "filled_quantity": filled_qty,
        "filled_price": filled_price,
        "order_confirmation": confirmation,
        "message": (
            f"승인형 {str(request_body.get('side', 'BUY')).upper()} pilot 전송 완료\n"
            f"broker_order_id={_mask(raw_order_id) or '미확인'}\n"
            f"broker_status={broker_status or '확인대기'} filled_qty={filled_qty:g}\n"
            "Hermes PASS + 자동 최종승인 경로\n"
            "live_order_sent=true"
        ),
    }


def submit_buy_order(request_body: dict, *, account_seq: str | None = None, timeout: float | None = None) -> dict:
    """하위 호환 alias. request_body['side']에 따라 BUY/SELL 모두 전송."""
    return submit_order(request_body, account_seq=account_seq, timeout=timeout)
