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
import math
import re
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)


def _is_exact_bool(value) -> bool:
    """exact bool 계약 — 문자열 "true"/"false"·정수 0/1은 schema invalid."""
    return type(value) is bool


def _exact_true(value) -> bool:
    """실행 허용은 exact True만. 그 외 모든 값(문자열·정수 포함)은 불허."""
    return value is True


def _finite_numeric(value) -> float | None:
    """bool/string을 숫자로 세탁하지 않는 finite 실행 경계 파서."""
    if isinstance(value, bool) or type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None

_ADAPTER_STATUS = "disabled"   # 코드 기본값 (env gate로 override 가능)

_VALID_SIDES = frozenset(["buy", "sell"])
_VALID_ORDER_TYPES = frozenset(["limit"])
_CLIENT_ORDER_ID_RE = re.compile(r"^tlive_[A-Za-z0-9_-]{1,30}$")

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

    max_krw = policy.get("max_order_krw")
    if max_krw and estimated_krw > max_krw and not blocks:
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

    # 0. 경계 객체 타입 계약 — 비-dict/None은 raise 없이 typed fail-closed
    boundary_violations = [
        f"{name}_schema_invalid: not_a_dict ({type(obj).__name__})"
        for name, obj in (
            ("policy", policy), ("preview", preview), ("payload", payload_result))
        if not isinstance(obj, dict)
    ]
    if boundary_violations:
        return False, boundary_violations

    # 1. policy gate — exact bool 계약: type is bool 필수, 허용은 is True만
    for key in ("live_pilot_enabled", "live_order_allowed", "autonomous_mode"):
        if not _is_exact_bool(policy.get(key)):
            reasons.append(
                f"policy_schema_invalid: {key}={policy.get(key)!r} (exact bool 필수)"
            )
    if not _exact_true(policy.get("live_pilot_enabled")):
        reasons.append("live_pilot_enabled=false")
    if not _exact_true(policy.get("live_order_allowed")):
        reasons.append("live_order_allowed=false")
    if policy.get("adapter_status") != "enabled":
        reasons.append(f"adapter_status={policy.get('adapter_status', 'disabled')}")
    # autonomous 모드(exact True)만 user confirmation 생략
    if not _exact_true(policy.get("autonomous_mode")):
        if not policy.get("requires_user_confirmation"):
            reasons.append("requires_user_confirmation missing")
        if not policy.get("requires_second_confirmation"):
            reasons.append("requires_second_confirmation missing")

    # 1.5 side guard (policy gate 다음, 나머지 guard 전에 체크)
    side = str(preview.get("side", "")).lower()
    allowed_sides = policy.get("allowed_sides", ["buy", "sell"])
    if side not in allowed_sides:
        reasons.append(f"side_not_allowed: side={side!r}")

    # 2. preview valid — ok/live_order_sent는 exact bool 계약
    if not _is_exact_bool(preview.get("ok")):
        reasons.append(
            f"preview_schema_invalid: ok={preview.get('ok')!r} (exact bool 필수)")
    if not _is_exact_bool(preview.get("live_order_sent", False)):
        reasons.append(
            "preview_schema_invalid: live_order_sent="
            f"{preview.get('live_order_sent')!r} (exact bool 필수)")
    if not _exact_true(preview.get("ok")):
        reasons.append("preview_not_ok")
    if preview.get("blocks"):
        reasons.append(f"preview_blocked: {preview['blocks']}")
    if preview.get("live_order_sent") is True:
        reasons.append("preview live_order_sent=true (duplicate guard)")

    # 3. payload valid — 같은 exact bool 계약
    if not _is_exact_bool(payload_result.get("ok")):
        reasons.append(
            f"payload_schema_invalid: ok={payload_result.get('ok')!r} (exact bool 필수)")
    if not _is_exact_bool(payload_result.get("live_order_sent", False)):
        reasons.append(
            "payload_schema_invalid: live_order_sent="
            f"{payload_result.get('live_order_sent')!r} (exact bool 필수)")
    if not _exact_true(payload_result.get("ok")):
        reasons.append("payload_not_ok")
    if payload_result.get("live_order_sent") is True:
        reasons.append("payload live_order_sent=true")

    # 4. symbol/asset guard
    symbol = str(preview.get("symbol", "")).strip()
    blocked_symbols = set(policy.get("blocked_symbols", []))
    if symbol in blocked_symbols:
        reasons.append(f"blocked_symbol: {symbol}")
    # digit-only 심볼은 항상 차단 (삼성증권 종목코드 형식)
    if not symbol or symbol.isdigit():
        reasons.append(f"invalid_or_digit_only_symbol: {symbol}")
    elif symbol.endswith((".KS", ".KQ")):
        # KR_STOCK이 허용 asset type에 있을 때만 통과
        if "KR_STOCK" not in policy.get("allowed_asset_types", []):
            reasons.append(f"non_us_symbol_not_allowed: {symbol}")
    # US_STOCK guard: .KS/.KQ가 아닌데 US_STOCK만 허용인 경우는 통과

    # 5. amount guard — None/0이면 임의 KRW cap 없음
    estimated = float(preview.get("estimated_amount_krw") or 0)
    max_krw = policy.get("max_order_krw")
    if max_krw:
        max_krw = float(max_krw)
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
        policy_for_daily = dict(policy)
        policy_for_daily["_current_side"] = side
        _daily_reasons = _check_daily_limits(symbol, estimated, policy_for_daily)
        reasons.extend(_daily_reasons)
    except Exception as e:
        log.warning("daily guard 조회 실패: %s", e)
        reasons.append("daily_guard_check_failed")

    # 9. autonomous 추가 가드
    if policy.get("autonomous_mode"):
        _auto_reasons = _check_autonomous_guards(symbol, side, estimated, limit_price, preview, policy)
        reasons.extend(_auto_reasons)

    ok = len(reasons) == 0
    return ok, reasons


def _check_autonomous_guards(
    symbol: str,
    side: str,
    estimated_krw: float,
    limit_price: float,
    preview: dict,
    policy: dict,
) -> list[str]:
    """Autonomous 모드 전용 가드: 한도/stop_loss/side 체크."""
    reasons: list[str] = []

    from core.toss_live_pilot_policy import classify_asset_type
    asset_type = classify_asset_type(symbol)

    # autonomous side 가드
    autonomous_sides = policy.get("autonomous_allowed_sides", ["buy", "sell"])
    if side not in autonomous_sides:
        reasons.append(f"autonomous_side_not_allowed: {side}")

    # KR_STOCK 한도
    if asset_type == "KR_STOCK":
        kr_max = policy.get("autonomous_kr_max_order_krw", 0)
        if kr_max and estimated_krw > kr_max:
            reasons.append(f"autonomous_kr_order_over_limit: {estimated_krw:,.0f} > {kr_max:,.0f}")

        # KR daily BUY cap
        if side == "buy":
            kr_daily_max = policy.get("autonomous_kr_max_daily_buy_krw", 0)
            if kr_daily_max:
                try:
                    today_kr_buy_total = _today_kr_buy_total()
                    if today_kr_buy_total + estimated_krw > kr_daily_max:
                        reasons.append(
                            f"autonomous_kr_daily_buy_over: "
                            f"{today_kr_buy_total + estimated_krw:,.0f} > {kr_daily_max:,.0f}"
                        )
                except Exception as e:
                    log.warning("KR daily buy check failed: %s", e)

    # US_STOCK 한도 (USD 기준)
    if asset_type == "US_STOCK":
        us_max = policy.get("autonomous_us_max_order_usd", 0)
        if us_max and limit_price > 0:
            qty = int(preview.get("quantity") or 0)
            order_usd = limit_price * qty
            if order_usd > us_max:
                reasons.append(f"autonomous_us_order_over_limit: ${order_usd:,.2f} > ${us_max:,.2f}")

    # BUY + stop_loss 필수
    if side == "buy":
        stop_loss = preview.get("stop_loss") or preview.get("invalidation")
        if not stop_loss:
            reasons.append("autonomous_buy_requires_stop_loss")

    return reasons


def _today_kr_buy_total() -> float:
    """당일 KR_STOCK BUY live_sent 총액."""
    from core.toss_live_pilot_ledger import list_live_pilot_records
    from core.toss_live_pilot_policy import classify_asset_type
    today = datetime.now(KST).strftime("%Y-%m-%d")
    records = list_live_pilot_records(limit=100)
    total = 0.0
    for r in records:
        if (
            r.get("status") == "live_sent"
            and r.get("created_at", "").startswith(today)
            and str(r.get("side", "")).lower() == "buy"
            and classify_asset_type(r.get("symbol", "")) == "KR_STOCK"
        ):
            total += float(r.get("estimated_amount_krw") or 0)
    return total


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

        # 주문 건수 제한: None/0/"unlimited" → 건수만으로는 차단하지 않음 (총액 cap으로만 관리)
        max_orders = policy.get("max_orders_per_day")
        if isinstance(max_orders, int) and max_orders > 0 and len(today_sent) >= max_orders:
            reasons.append(f"daily_order_count_exceeded: {len(today_sent)}/{max_orders}")

        max_daily = policy.get("max_daily_krw")
        today_total = sum(float(r.get("estimated_amount_krw") or 0) for r in today_sent)
        if max_daily:
            max_daily = float(max_daily)
            if today_total + estimated_krw > max_daily:
                reasons.append(
                    f"daily_amount_exceeded: {today_total + estimated_krw:,.0f} > {max_daily:,.0f}"
                )

        # 중복 주문 체크: 같은 symbol+same side만 차단한다.
        # BUY 후 SELL 왕복 테스트/리스크 청산은 허용해야 하므로 symbol 단독 중복으로 막지 않는다.
        current_side = str(policy.get("_current_side") or "").lower()
        today_sent_keys = {(r.get("symbol"), str(r.get("side") or "").lower()) for r in today_sent}
        if current_side and (symbol, current_side) in today_sent_keys:
            reasons.append(f"duplicate_symbol_side_today: {symbol}/{current_side}")

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
    # 0. 경계 객체 타입 계약 — 비-dict/None은 raise 없이 typed fail-closed
    if not isinstance(payload, dict) or not isinstance(policy, dict):
        return {
            "ok": False,
            "blocked": True,
            "reason": "dispatch_contract_invalid",
            "live_order_sent": False,
            "failure_reason": (
                "dispatch_contract_invalid: "
                f"payload={type(payload).__name__}, policy={type(policy).__name__}"
            ),
            "symbol": "unknown",
            "message": "dispatch 입력 계약 위반 — 전송 안 함\nlive_order_sent=false",
        }

    symbol = payload.get("symbol", "unknown")

    # transport 자체가 없으면 payload를 해석할 이유 없이 기존 typed block을 유지한다.
    # callable이 주입된 경우에만 아래 독립 주문 계약을 검증한다.
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

    # 0.5 transport 호출 전 독립 계약 검증 — 빈/불량 policy·payload는
    # can_send 선행 여부와 무관하게 여기서 한 번 더 차단한다 (defense in depth).
    contract_violations: list[str] = []
    for key in ("live_pilot_enabled", "live_order_allowed", "autonomous_mode"):
        if not _is_exact_bool(policy.get(key)):
            contract_violations.append(f"policy.{key}_not_exact_bool")
    if not _exact_true(policy.get("live_pilot_enabled")):
        contract_violations.append("policy.live_pilot_enabled_not_true")
    if not _exact_true(policy.get("live_order_allowed")):
        contract_violations.append("policy.live_order_allowed_not_true")
    if policy.get("adapter_status") != "enabled":
        contract_violations.append("policy.adapter_status_not_enabled")
    raw_symbol = payload.get("symbol")
    sym_text = raw_symbol.strip().upper() if isinstance(raw_symbol, str) else ""
    if not sym_text or sym_text.isdigit():
        contract_violations.append("payload.symbol_invalid")
    raw_blocked_symbols = policy.get("blocked_symbols", [])
    if not isinstance(raw_blocked_symbols, (list, tuple, set, frozenset)):
        contract_violations.append("policy.blocked_symbols_invalid")
        blocked_symbols = set()
    else:
        blocked_symbols = {
            str(value).strip().upper() for value in raw_blocked_symbols
        }
    if sym_text and sym_text in blocked_symbols:
        contract_violations.append("payload.symbol_blocked")

    raw_allowed_assets = policy.get(
        "allowed_asset_types", ["KR_STOCK", "US_STOCK"],
    )
    if not isinstance(raw_allowed_assets, (list, tuple, set, frozenset)):
        contract_violations.append("policy.allowed_asset_types_invalid")
        allowed_assets = set()
    else:
        allowed_assets = {
            str(value).strip().upper() for value in raw_allowed_assets
        }
    asset_type = "KR_STOCK" if sym_text.endswith((".KS", ".KQ")) else "US_STOCK"
    if sym_text and asset_type not in allowed_assets:
        contract_violations.append("payload.asset_type_not_allowed")

    raw_side = payload.get("side")
    side = raw_side.strip().lower() if isinstance(raw_side, str) else ""
    if side not in _VALID_SIDES:
        contract_violations.append("payload.side_invalid")
    allowed_sides = policy.get("allowed_sides", list(_VALID_SIDES))
    if not isinstance(allowed_sides, (list, tuple, set, frozenset)):
        contract_violations.append("policy.allowed_sides_invalid")
    elif side and side not in {str(value).strip().lower() for value in allowed_sides}:
        contract_violations.append("payload.side_not_allowed")

    if str(payload.get("order_type") or "").strip().lower() not in _VALID_ORDER_TYPES:
        contract_violations.append("payload.order_type_invalid")

    qty_value = _finite_numeric(payload.get("quantity"))
    if (
        qty_value is None
        or qty_value <= 0
        or not qty_value.is_integer()
    ):
        contract_violations.append("payload.quantity_invalid")

    price_value = _finite_numeric(payload.get("limit_price"))
    if (
        price_value is None
        or price_value <= 0
    ):
        contract_violations.append("payload.limit_price_invalid")

    if policy.get("autonomous_mode") is True:
        kill_switch = policy.get("autonomous_kill_switch")
        if not _is_exact_bool(kill_switch):
            contract_violations.append("policy.autonomous_kill_switch_not_exact_bool")
        elif kill_switch is True:
            contract_violations.append("policy.autonomous_kill_switch_active")
        autonomous_sides = policy.get("autonomous_allowed_sides", ["buy"])
        if not isinstance(autonomous_sides, (list, tuple, set, frozenset)):
            contract_violations.append("policy.autonomous_allowed_sides_invalid")
        elif side and side not in {
            str(value).strip().lower() for value in autonomous_sides
        }:
            contract_violations.append("payload.autonomous_side_not_allowed")
        client_order_id = payload.get("client_order_id")
        pilot_id = payload.get("pilot_id")
        if (
            not isinstance(client_order_id, str)
            or not _CLIENT_ORDER_ID_RE.fullmatch(client_order_id)
            or not isinstance(pilot_id, str)
            or pilot_id != client_order_id
        ):
            contract_violations.append("payload.autonomous_order_ids_invalid")
    if contract_violations:
        log.warning(
            "live dispatch blocked before transport (contract): %s",
            contract_violations)
        return {
            "ok": False,
            "blocked": True,
            "reason": "dispatch_contract_invalid",
            "live_order_sent": False,
            "failure_reason": "dispatch_contract_invalid: " + ", ".join(contract_violations),
            "symbol": symbol,
            "message": "dispatch 계약 위반 — transport 미호출\nlive_order_sent=false",
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

    # transport 결과도 신뢰하지 않는다 — 비-dict/None은 raise 없이 fail-closed
    if not isinstance(transport_result, dict):
        log.error(
            "transport returned non-dict (fail-closed): %s",
            type(transport_result).__name__)
        return {
            "ok": False,
            "blocked": False,
            "reason": "transport_schema_invalid",
            "live_order_sent": False,
            "broker_confirmed": False,
            "failure_reason": (
                f"transport_schema_invalid: result={type(transport_result).__name__}"
            ),
            "symbol": symbol,
            "payload_hash": payload_hash,
            "message": "transport 응답이 dict가 아님 — 전송 여부 불명, fail-closed\n"
                       "live_order_sent=false",
        }
    # ok/live_order_sent/broker_confirmed는 exact bool 계약.
    # 타입 위반은 transport_schema_invalid + live_order_sent=False.
    schema_violations = [
        key for key in ("ok", "live_order_sent", "broker_confirmed")
        if not _is_exact_bool(transport_result.get(key, False))
    ]
    if schema_violations:
        log.error(
            "transport schema invalid (fail-closed): fields=%s", schema_violations)
        return {
            "ok": False,
            "blocked": False,
            "reason": "transport_schema_invalid",
            "live_order_sent": False,
            "broker_confirmed": False,
            "failure_reason": (
                "transport_schema_invalid: "
                + ", ".join(
                    f"{k}={transport_result.get(k)!r}" for k in schema_violations)
            ),
            "symbol": symbol,
            "payload_hash": payload_hash,
            "message": "transport 응답 스키마 위반 — 전송 여부 불명, fail-closed\n"
                       "live_order_sent=false",
        }
    sent = (
        transport_result.get("ok") is True
        and transport_result.get("live_order_sent") is True
    )
    broker_order_id = transport_result.get("broker_order_id", "")
    # broker_order_id에서 민감 패턴 제거 (accountNo 형식 등)
    import re as _re
    broker_order_id = _re.sub(r'\d{8}-\d{2}', "[masked]", str(broker_order_id))

    result = {
        "ok": sent,
        "blocked": False,
        "reason": (
            transport_result.get("reason")
            or transport_result.get("failure_reason")
            or ("live_sent" if sent else "unknown")
        ),
        "live_order_sent": sent,
        "adapter_status": policy.get("adapter_status", "disabled"),
        "live_order_allowed": policy.get("live_order_allowed", False),
        "symbol": symbol,
        "quantity": payload.get("quantity"),
        "limit_price": payload.get("limit_price"),
        "estimated_amount_krw": payload.get("estimated_amount_krw"),
        "broker_order_id": broker_order_id,
        "broker_confirmed": transport_result.get("broker_confirmed") is True,
        "broker_order_status": transport_result.get("broker_order_status", ""),
        "filled_quantity": transport_result.get("filled_quantity", 0.0),
        "filled_price": transport_result.get("filled_price", 0.0),
        "order_confirmation": transport_result.get("order_confirmation", {}),
        "payload_hash": payload_hash,
        "transport_status": transport_result.get("status", "") or transport_result.get("transport_status", ""),
        "failure_reason": (
            transport_result.get("failure_reason")
            or transport_result.get("reason")
            or ""
        ) if not sent else "",
    }
    if transport_result.get("error_body"):
        result["error_body"] = transport_result.get("error_body")
    order_request_preview = (
        transport_result.get("order_request_preview")
        or transport_result.get("request_preview")
    )
    if order_request_preview:
        result["order_request_preview"] = order_request_preview

    if sent:
        autonomous_policy = bool(
            policy.get("autonomous_mode") is True
            and policy.get("requires_user_confirmation") is not True
            and policy.get("requires_second_confirmation") is not True
        )
        execution_contract = (
            "Toss AI autonomous 실행\n"
            "Hermes PASS + 결정론 안전 게이트\n"
            if autonomous_policy
            else "Hermes 승인형 자동실행 범위\nHermes PASS + 사용자 최종 승인 1건\n"
        )
        result["message"] = (
            f"{'Toss AI autonomous' if autonomous_policy else '승인형'} "
            f"{str(payload.get('side', 'buy')).upper()} pilot 전송 완료\n"
            f"{execution_contract}"
            f"broker_order_id={broker_order_id or '미확인'}\n"
            f"broker_status={result.get('broker_order_status') or '확인대기'} filled_qty={float(result.get('filled_quantity') or 0):g}\n"
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
