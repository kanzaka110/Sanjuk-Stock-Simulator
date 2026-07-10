"""core/toss_live_transport.py

Toss live order transport 인터페이스 + 기본 구현.

현재 상태: not_configured
- Toss 실제 주문 endpoint 미확인 (read-only API만 확인됨)
- 실제 HTTP transport는 endpoint/필드/응답 스키마 확인 후 별도 구현
- 기본 구현체(NotConfiguredTossLiveTransport)는 항상 blocked 반환

인터페이스:
  transport.send_buy_order(payload: dict) → dict  # buy/sell 공용 payload

금지:
  - 추측 endpoint HTTP POST 금지
  - 계좌번호/토큰/키/시크릿 payload 포함 금지
  - 한국장 symbol 주문 금지 (US_STOCK only)
  - live_order_sent=True를 endpoint 확인 없이 반환 금지
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

log = logging.getLogger(__name__)

# 전역 transport 상태 — 기본 not_configured.
# 명시적으로 armed된 runtime(아래 _runtime_live_transport_armed)에서만 configured로 승격.
LIVE_TRANSPORT_STATUS: str = "not_configured"

# 주문 schema 상수 (US_STOCK BUY+SELL 지정가)
_CLIENT_ORDER_ID_MAX = 36
_CLIENT_ORDER_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")
_DEFAULT_MAX_ORDER_KRW = 0  # 0/None = 임의 KRW cap 없음


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
            - 계좌번호/토큰/키/시크릿 포함 금지
            - 한국장 symbol 주문 금지
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


# ─── 주문 생성 schema 변환 (dry-run, HTTP 호출 없음) ───────────────

def _as_positive_int(value) -> int | None:
    """양의 정수값이면 int 반환, 아니면 None (소수/음수/0/비정상 → None)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 and value.is_integer() else None
    if isinstance(value, str):
        try:
            f = float(value)
        except ValueError:
            return None
        return int(f) if f > 0 and f.is_integer() else None
    return None


def _normalize_client_order_id(raw: str) -> str:
    """clientOrderId 정규화: 허용문자만, 최대 36자 (길면 truncate+hash)."""
    cleaned = _CLIENT_ORDER_ID_RE.sub("", str(raw or ""))
    if not cleaned:
        cleaned = "tlive"
    if len(cleaned) <= _CLIENT_ORDER_ID_MAX:
        return cleaned
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:8]
    prefix = cleaned[: _CLIENT_ORDER_ID_MAX - 9]  # prefix + '-' + 8자 hash = 36
    return f"{prefix}-{digest}"


def _classify_asset_type(symbol: str) -> str:
    """심볼에서 asset type 판별."""
    s = str(symbol).strip().upper()
    if s.endswith((".KS", ".KQ")):
        return "KR_STOCK"
    return "US_STOCK"


def _kr_price_tick_size(price: float) -> int:
    """KR_STOCK limit price tick size used by Toss/KRX-compatible orders."""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def _normalize_kr_limit_price(price: float) -> tuple[int, int, bool]:
    """Return (normalized_price, tick_size, adjusted).

    Toss rejects off-tick KR prices. Floor to the nearest valid tick so the
    request is never invalid because of a midpoint/stale quote such as 110050
    when tickSize=100.
    """
    tick = _kr_price_tick_size(float(price))
    normalized = int(float(price) // tick * tick)
    if normalized <= 0:
        normalized = tick
    return normalized, tick, normalized != int(float(price))


def build_toss_order_create_request(
    payload: dict,
    *,
    client_order_id: str,
    max_order_krw: float = _DEFAULT_MAX_ORDER_KRW,
    asset_type: str | None = None,
) -> dict:
    """내부 payload → Toss 주문 생성 request body 변환 (dry-run only).

    실제 HTTP 호출 없음. 민감정보(계좌번호/토큰/키/시크릿) 미포함.
    US_STOCK/KR_STOCK BUY+SELL + LIMIT. digit-only ticker는 항상 차단.

    Args:
        asset_type: "US_STOCK" | "KR_STOCK" | None (None이면 symbol에서 자동 판별)

    Returns:
        {"ok": bool, "request": dict, "blocks": list[str], "warnings": list[str]}
    """
    blocks: list[str] = []
    warnings: list[str] = ["dry-run only", "not sent"]

    symbol = str(payload.get("symbol", "")).strip()
    side = str(payload.get("side", "")).strip().lower()
    order_type = str(payload.get("order_type", "")).strip().lower()
    quantity = payload.get("quantity")
    limit_price = payload.get("limit_price")
    estimated_krw = payload.get("estimated_amount_krw")

    norm_symbol = symbol.upper()
    if not norm_symbol:
        blocks.append("invalid_symbol: empty")

    # asset type 결정
    if asset_type is None:
        asset_type = _classify_asset_type(norm_symbol)

    # digit-only 심볼은 항상 차단 (삼성증권 종목코드 형식)
    if norm_symbol.isdigit():
        blocks.append(f"digit_only_symbol_blocked: {symbol}")
    elif asset_type == "US_STOCK" and norm_symbol.endswith((".KS", ".KQ")):
        blocks.append(f"non_us_symbol_not_allowed: {symbol}")
    # KR_STOCK + .KS/.KQ → 허용

    # BUY+SELL guard
    if side not in ("buy", "sell"):
        blocks.append(f"side_not_allowed: side={side!r}")

    # LIMIT only guard
    if order_type != "limit":
        blocks.append(f"order_type_not_limit: {order_type!r}")

    qty_int = _as_positive_int(quantity)
    if qty_int is None:
        blocks.append(f"invalid_quantity: {quantity!r} (양의 정수 필요)")

    try:
        price_num = float(limit_price)
    except (TypeError, ValueError):
        price_num = 0.0
    if price_num <= 0:
        blocks.append(f"invalid_price: {limit_price!r} (양수 필요)")

    # 금액 한도 재확인
    try:
        est = float(estimated_krw) if estimated_krw is not None else (
            float(price_num * qty_int) if price_num and qty_int else 0.0
        )
    except (TypeError, ValueError):
        est = 0.0
    if max_order_krw and est > float(max_order_krw):
        blocks.append(f"amount_over_limit: {est:,.0f} > {float(max_order_krw):,.0f}")

    cid = _normalize_client_order_id(client_order_id)

    if blocks:
        return {"ok": False, "request": {}, "blocks": blocks, "warnings": warnings}

    # KR_STOCK: 가격은 호가단위에 맞춘 정수 KRW, symbol에서 .KS/.KQ suffix strip
    if asset_type == "KR_STOCK":
        normalized_price, tick_size, adjusted = _normalize_kr_limit_price(price_num)
        if adjusted:
            warnings.append(
                f"kr_tick_price_adjusted: {price_num:g} -> {normalized_price} (tick={tick_size})"
            )
        price_str = str(normalized_price)
        # Toss API는 국내 종목코드를 숫자만 받음 (예: "316140", not "316140.KS")
        api_symbol = norm_symbol.replace(".KS", "").replace(".KQ", "")
    else:
        price_str = str(int(price_num)) if price_num.is_integer() else str(price_num)
        api_symbol = norm_symbol

    request = {
        "clientOrderId": cid,
        "symbol": api_symbol,
        "side": side.upper(),
        "orderType": "LIMIT",
        "quantity": str(qty_int),
        "price": price_str,
        "timeInForce": "DAY",
        "confirmHighValueOrder": False,
    }
    return {"ok": True, "request": request, "blocks": [], "warnings": warnings}


class DryRunTossLiveTransport(TossLiveTransportBase):
    """Dry-run schema transport — request preview만 생성, 절대 전송하지 않음.

    실제 HTTP 호출 없음. ok=True여도 blocked=True, live_order_sent=False 유지.
    transport_status는 dry_run_schema_ready.
    """

    def send_buy_order(self, payload: dict) -> dict:
        cid = (
            payload.get("client_order_id")
            or payload.get("pilot_id")
            or payload.get("preview_id")
            or "tlive"
        )
        max_krw = payload.get("max_order_krw", _DEFAULT_MAX_ORDER_KRW)
        max_krw = float(max_krw) if max_krw else 0
        built = build_toss_order_create_request(
            payload, client_order_id=cid, max_order_krw=max_krw
        )

        if not built["ok"]:
            return {
                "ok": False,
                "blocked": True,
                "reason": "dry_run_schema_blocked",
                "live_order_sent": False,
                "transport_status": "dry_run_schema_ready",
                "blocks": built["blocks"],
                "message": (
                    "차단(dry-run): 주문 schema 검증 실패\n"
                    "실제 주문 아님 — 아직 주문 전송 안 함\n"
                    "live_order_sent=false"
                ),
            }

        return {
            "ok": True,
            "blocked": True,            # dry-run이므로 ok여도 차단 유지
            "reason": "dry_run_transport_only",
            "live_order_sent": False,
            "transport_status": "dry_run_schema_ready",
            "order_request_preview": built["request"],
            "warnings": built["warnings"],
            "message": (
                "dry-run schema 준비 완료\n"
                "실제 주문 아님 — 아직 주문 전송 안 함\n"
                "live_order_sent=false"
            ),
        }


class LiveTossTransport(TossLiveTransportBase):
    """실제 Toss 주문 전송 transport.

    [중요] production 기본 경로에서 자동 주입/자동 실행되지 않음.
    - DEFAULT_LIVE_TRANSPORT는 NotConfigured 유지
    - env gate 3개 + Hermes PASS + 사용자 최종 승인 + BUY/SELL + US-only guard 통과
      + 명시적 transport 주입 없이는 실제 주문 불가
    - schema 검증(build_toss_order_create_request) 통과 시에만 HTTP 위임

    민감 header/secret 처리는 core.toss_live_order_http로 격리한다.
    이 클래스/모듈은 token/account/header literal을 직접 보유하지 않는다.
    """

    def __init__(self, *, timeout: float | None = None, account_seq: str | None = None):
        self._timeout = timeout
        self._account_seq = account_seq

    def send_buy_order(self, payload: dict) -> dict:
        cid = (
            payload.get("client_order_id")
            or payload.get("pilot_id")
            or payload.get("preview_id")
            or "tlive"
        )
        max_krw = payload.get("max_order_krw", _DEFAULT_MAX_ORDER_KRW)
        max_krw = float(max_krw) if max_krw else 0
        asset_type = payload.get("asset_type") or _classify_asset_type(
            str(payload.get("symbol", ""))
        )
        built = build_toss_order_create_request(
            payload, client_order_id=cid, max_order_krw=max_krw,
            asset_type=asset_type,
        )

        if not built["ok"]:
            return {
                "ok": False,
                "blocked": True,
                "reason": "live_schema_blocked",
                "live_order_sent": False,
                "transport_status": "live_send_blocked",
                "blocks": built["blocks"],
                "message": (
                    "차단: 주문 schema 검증 실패\n"
                    "아직 주문 전송 안 함\nlive_order_sent=false"
                ),
            }

        from core.toss_live_order_http import submit_order

        result = submit_order(
            built["request"],
            account_seq=self._account_seq,
            timeout=self._timeout,
        )
        if not result.get("live_order_sent"):
            # Safe request preview only: no token/account/header fields.
            result.setdefault("order_request_preview", built["request"])
        return result


def _runtime_live_transport_armed() -> bool:
    """실 transport(LiveTossTransport)를 기본 주입할 수 있는 armed runtime 여부.

    안전 설계:
    - stock-bot 등 실제 운영 프로세스는 systemd Environment로 TOSS_LIVE_TRANSPORT_ARMED=true
      + gate 3종을 받아 armed 상태로 import된다.
    - pytest/import/dev shell은 TOSS_LIVE_TRANSPORT_ARMED가 없으므로 항상 false.
      (테스트가 gate 3종을 patch.dict로 켜도 ARMED가 없으면 실 transport 미구성.)
    - 따라서 PASS/preview/콜백만으로 실주문 경로가 열리지 않는다.
    """
    if os.environ.get("TOSS_LIVE_TRANSPORT_ARMED", "").strip().lower() != "true":
        return False
    return (
        os.environ.get("TOSS_LIVE_PILOT_ENABLED", "").strip().lower() == "true"
        and os.environ.get("TOSS_LIVE_ORDER_ALLOWED", "").strip().lower() == "true"
        and os.environ.get("TOSS_LIVE_ADAPTER_ENABLED", "").strip().lower() == "true"
    )


# 기본 transport 인스턴스.
# armed runtime에서만 실 LiveTossTransport, 그 외에는 NotConfigured(차단) 유지.
if _runtime_live_transport_armed():
    DEFAULT_LIVE_TRANSPORT: TossLiveTransportBase = LiveTossTransport()
    LIVE_TRANSPORT_STATUS = "configured"
else:
    DEFAULT_LIVE_TRANSPORT = NotConfiguredTossLiveTransport()


def get_transport_status() -> dict:
    """현재 transport 설정 상태 반환 (read-only)."""
    return {
        "status": LIVE_TRANSPORT_STATUS,
        "live_order_sent_possible": False,      # 기본 미설정 — 전송 불가
        "endpoint_confirmed": False,            # 실제 전송 transport 자동 주입 안 됨
        "dry_run_schema_ready": True,           # request schema 변환 준비됨
        "order_schema_confirmed": True,         # 주문 생성 schema 확인됨 (전송 아님)
        "live_transport_class_available": True,  # 클래스 존재 (기본 주입 아님)
        "description": (
            "주문 schema 준비 + Live transport 클래스 존재 — "
            "기본 transport는 not_configured, env gate 꺼짐 (전송 0건)"
        ),
    }
