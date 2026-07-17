"""core/toss_live_pilot_policy.py

Toss Live Pilot 정책 모듈 (read-only, fail-closed).

모드:
  approval_only_live_pilot  — 기존 수동 승인 (Telegram 버튼)
  autonomous_live_pilot     — Hermes PASS → 자동 실행

활성화 조건 (3개 env 모두 true여야):
  TOSS_LIVE_PILOT_ENABLED=true
  TOSS_LIVE_ORDER_ALLOWED=true
  TOSS_LIVE_ADAPTER_ENABLED=true

자율실행 추가 조건:
  TOSS_AUTONOMOUS_MODE=true
  TOSS_AUTONOMOUS_KILL_SWITCH=false (true면 모든 자율 주문 차단)

이 중 하나라도 누락/false이면 adapter_status=disabled, live_order_allowed=False.

금지:
- 이 모듈에서 주문 API 직접 호출 금지
- 민감정보(key/secret/accountNo) 출력 금지
"""

from __future__ import annotations

import os
import logging

log = logging.getLogger(__name__)

# ── 정책 상수 ──────────────────────────────────────────────────────
_SAMPLE_LIVE_THRESHOLD = 5          # evaluated_count < 5 → 표본부족 경고
_MAX_ORDER_KRW = None               # US 자동화: 임의 KRW 1회 한도 없음
_MAX_DAILY_KRW = None               # US 자동화: 임의 KRW 1일 한도 없음
_MAX_ORDERS_PER_DAY = None          # 주문 건수 제한 없음

_BLOCKED_SYMBOLS: frozenset[str] = frozenset()
_LIVE_ALLOWED_SYMBOLS: list[str] = []
_PREFERRED_SYMBOLS: list[str] = ["069500.KS"]

_ALLOWED_ASSET_TYPES: list[str] = ["US_STOCK"]

# BUY+SELL 정책
_LIVE_PILOT_SIDE_MODE: str = "BUY_SELL"
_LIVE_PILOT_ALLOWED_SIDES: list[str] = ["buy", "sell"]
_LIVE_PILOT_BLOCK_SELL: bool = False

# ── Autonomous 기본값 ─────────────────────────────────────────────
# 0 = 임의 금액 한도 없음 (2026-07-04 사용자 승인으로 KRW/USD cap 제거).
# 나머지 안전장치(kill switch/Hermes PASS/품질게이트/stop_loss 필수/중복 방지)는 유지.
_AUTONOMOUS_KR_MAX_ORDER_KRW = 0
_AUTONOMOUS_KR_MAX_DAILY_BUY_KRW = 0
_AUTONOMOUS_US_MAX_ORDER_USD = 0
_AUTONOMOUS_SYMBOL_MAX_WEIGHT_PCT = 15


# ── 내부 helpers ───────────────────────────────────────────────────

def _env_bool(key: str, default: bool = False) -> bool:
    """환경변수를 bool로 읽기. 미설정/빈값 → default."""
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes", "on")


def _env_int(key: str, default: int) -> int:
    """환경변수를 int로 읽기. 파싱 실패 → default."""
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_csv(key: str, default: str) -> list[str]:
    """환경변수를 콤마 구분 리스트로 읽기."""
    val = os.environ.get(key, "").strip()
    if not val:
        val = default
    return [s.strip() for s in val.split(",") if s.strip()]


def _is_live_pilot_env_enabled() -> bool:
    return _env_bool("TOSS_LIVE_PILOT_ENABLED")


def _is_live_order_allowed_env() -> bool:
    return _env_bool("TOSS_LIVE_ORDER_ALLOWED")


def _is_live_adapter_enabled_env() -> bool:
    return _env_bool("TOSS_LIVE_ADAPTER_ENABLED")


def _all_live_gates_open() -> bool:
    return (
        _is_live_pilot_env_enabled()
        and _is_live_order_allowed_env()
        and _is_live_adapter_enabled_env()
    )


# ── Autonomous env readers ────────────────────────────────────────

def _is_autonomous_mode() -> bool:
    return _env_bool("TOSS_AUTONOMOUS_MODE")


def _is_autonomous_kill_switch() -> bool:
    return _env_bool("TOSS_AUTONOMOUS_KILL_SWITCH")


def _get_autonomous_allowed_asset_types() -> list[str]:
    return _env_csv("TOSS_AUTONOMOUS_ALLOWED_ASSET_TYPES", "US_STOCK")


def _get_autonomous_allowed_sides() -> list[str]:
    return [s.lower() for s in _env_csv("TOSS_AUTONOMOUS_ALLOWED_SIDES", "BUY,SELL")]


def _get_evaluated_count() -> int:
    try:
        from core.toss_paper_performance import get_paper_performance_summary
        s = get_paper_performance_summary().get("summary", {})
        return int(s.get("evaluated_count", 0))
    except Exception as e:
        log.debug("evaluated_count 조회 실패: %s", e)
        return 0


# ── Asset type 판별 ───────────────────────────────────────────────

def classify_asset_type(symbol: str) -> str:
    """심볼에서 asset type 판별. .KS/.KQ → KR_STOCK, 그 외 → US_STOCK."""
    s = str(symbol).strip().upper()
    if s.endswith((".KS", ".KQ")):
        return "KR_STOCK"
    return "US_STOCK"


# ── 공개 API ───────────────────────────────────────────────────────

def compute_toss_live_pilot_policy(
    evaluated_count: int | None = None,
) -> dict:
    """Live pilot 정책 계산.

    3개 env gate 모두 true → adapter enabled.
    TOSS_AUTONOMOUS_MODE=true → 자율실행 모드 (Hermes PASS = 자동 주문).
    TOSS_AUTONOMOUS_KILL_SWITCH=true → 자율 주문 전체 차단.

    Returns:
        policy dict
    """
    if evaluated_count is None:
        evaluated_count = _get_evaluated_count()

    env_pilot = _is_live_pilot_env_enabled()
    env_order = _is_live_order_allowed_env()
    env_adapter = _is_live_adapter_enabled_env()
    all_gates = _all_live_gates_open()
    insufficient = evaluated_count < _SAMPLE_LIVE_THRESHOLD

    # autonomous mode
    autonomous = _is_autonomous_mode()
    kill_switch = _is_autonomous_kill_switch()
    autonomous_asset_types = _get_autonomous_allowed_asset_types()
    autonomous_sides = _get_autonomous_allowed_sides()

    max_order_krw = _MAX_ORDER_KRW
    if insufficient:
        warnings: list[str] = [
            "Paper 표본부족 — Hermes PASS 필수"
        ]
    else:
        warnings = []

    # adapter 활성화: 3개 env gate 모두 통과해야
    if all_gates:
        adapter_status = "enabled"
        live_pilot_enabled = True
        live_order_allowed = True
        block_reason = ""
    else:
        adapter_status = "disabled"
        live_pilot_enabled = False
        live_order_allowed = False
        missing = []
        if not env_pilot:
            missing.append("TOSS_LIVE_PILOT_ENABLED")
        if not env_order:
            missing.append("TOSS_LIVE_ORDER_ALLOWED")
        if not env_adapter:
            missing.append("TOSS_LIVE_ADAPTER_ENABLED")
        block_reason = "env gate 미충족: " + ", ".join(missing) if missing else "env gate 미충족"

    # kill switch → 자율 주문 차단
    if autonomous and kill_switch:
        live_order_allowed = False
        block_reason = "autonomous_kill_switch_active"
        warnings.append("자율실행 킬스위치 활성 — 모든 자율 주문 차단")

    # mode / confirmation 결정
    if autonomous and not kill_switch:
        mode = "autonomous_live_pilot"
        requires_user_confirmation = False
        requires_second_confirmation = False
        reason = "자율실행 모드 — Hermes PASS 시 자동 주문"
    else:
        mode = "approval_only_live_pilot"
        requires_user_confirmation = True
        requires_second_confirmation = True
        reason = "승인형 live pilot — 수동 최종 승인 전용 (기본: 비활성)"

    # allowed_asset_types 결합
    effective_asset_types = list(_ALLOWED_ASSET_TYPES)
    if autonomous:
        for at in autonomous_asset_types:
            if at not in effective_asset_types:
                effective_asset_types.append(at)

    policy: dict = {
        "mode": mode,
        "live_pilot_enabled": live_pilot_enabled,
        "live_order_allowed": live_order_allowed,
        "adapter_status": adapter_status,
        "requires_user_confirmation": requires_user_confirmation,
        "requires_second_confirmation": requires_second_confirmation,
        # env 상태
        "env_live_pilot_enabled": env_pilot,
        "env_live_order_allowed": env_order,
        "env_live_adapter_enabled": env_adapter,
        "all_live_gates_open": all_gates,
        # 한도
        "max_order_krw": max_order_krw,
        "max_daily_krw": _MAX_DAILY_KRW,
        "daily_krw_is_cap": False,
        "daily_krw_is_target": False,
        "max_orders_per_day": _MAX_ORDERS_PER_DAY,
        "max_orders_per_day_label": "unlimited",
        "order_count_limited": False,
        "daily_policy_note": (
            "BUY+SELL. 임의 KRW 일일 목표/상한 없음. "
            "이미 환전된 USD buying power와 보유수량 안에서만 실행한다."
        ),
        # 종목
        "allowed_asset_types": effective_asset_types,
        "blocked_symbols": sorted(_BLOCKED_SYMBOLS),
        "live_allowed_symbols": _LIVE_ALLOWED_SYMBOLS,
        "preferred_symbols": _PREFERRED_SYMBOLS,
        # 상태
        "evaluated_count": evaluated_count,
        "sample_insufficient": insufficient,
        "warnings": warnings,
        "block_reason": block_reason,
        "reason": reason,
        # BUY+SELL 정책
        "side_mode": _LIVE_PILOT_SIDE_MODE,
        "allowed_sides": list(_LIVE_PILOT_ALLOWED_SIDES),
        "sell_allowed": True,
        # transport
        "live_transport_status": _get_live_transport_status(),
        # autonomous
        "autonomous_mode": autonomous,
        "autonomous_kill_switch": kill_switch,
        "autonomous_allowed_asset_types": autonomous_asset_types,
        "autonomous_allowed_sides": autonomous_sides,
        "autonomous_kr_max_order_krw": _env_int(
            "TOSS_AUTONOMOUS_KR_MAX_ORDER_KRW", _AUTONOMOUS_KR_MAX_ORDER_KRW,
        ),
        "autonomous_kr_max_daily_buy_krw": _env_int(
            "TOSS_AUTONOMOUS_KR_MAX_DAILY_BUY_KRW", _AUTONOMOUS_KR_MAX_DAILY_BUY_KRW,
        ),
        "autonomous_us_max_order_usd": _env_int(
            "TOSS_AUTONOMOUS_US_MAX_ORDER_USD", _AUTONOMOUS_US_MAX_ORDER_USD,
        ),
        "autonomous_symbol_max_weight_pct": _env_int(
            "TOSS_AUTONOMOUS_SYMBOL_MAX_WEIGHT_PCT", _AUTONOMOUS_SYMBOL_MAX_WEIGHT_PCT,
        ),
    }

    return policy


def validate_autonomous_execution_policy(
    policy: object,
    *,
    side: str | None = None,
) -> tuple[bool, str]:
    """주문 경계가 신뢰할 수 있는 autonomous live policy인지 exact 검증."""
    if type(policy) is not dict:
        return False, "policy_contract_invalid"
    if policy.get("autonomous_mode") is False:
        return False, "autonomous_mode_disabled"
    if policy.get("autonomous_mode") is not True:
        return False, "policy_contract_invalid"
    if policy.get("autonomous_kill_switch") is True:
        return False, "kill_switch_active"
    if policy.get("autonomous_kill_switch") is not False:
        return False, "policy_contract_invalid"

    true_fields = (
        "live_pilot_enabled",
        "live_order_allowed",
        "all_live_gates_open",
        "env_live_pilot_enabled",
        "env_live_order_allowed",
        "env_live_adapter_enabled",
    )
    false_fields = (
        "requires_user_confirmation",
        "requires_second_confirmation",
    )
    if (
        policy.get("mode") != "autonomous_live_pilot"
        or policy.get("adapter_status") != "enabled"
        or policy.get("live_transport_status") != "configured"
        or policy.get("side_mode") != _LIVE_PILOT_SIDE_MODE
        or type(policy.get("allowed_sides")) is not list
        or policy.get("allowed_sides") != list(_LIVE_PILOT_ALLOWED_SIDES)
        or policy.get("sell_allowed") is not True
        or any(policy.get(field) is not True for field in true_fields)
        or any(policy.get(field) is not False for field in false_fields)
    ):
        return False, "policy_contract_invalid"

    raw_sides = policy.get("autonomous_allowed_sides")
    if (
        type(raw_sides) is not list
        or not raw_sides
        or any(type(value) is not str or value not in {"buy", "sell"} for value in raw_sides)
        or len(raw_sides) != len(set(raw_sides))
    ):
        return False, "policy_contract_invalid"
    if side is not None:
        if type(side) is not str or side not in {"buy", "sell"}:
            return False, "policy_contract_invalid"
        if side not in raw_sides:
            return False, f"{side}_not_allowed_by_env"
    return True, ""


def _get_live_transport_status() -> str:
    """Toss live transport 설정 상태 반환 (read-only)."""
    try:
        from core.toss_live_transport import LIVE_TRANSPORT_STATUS
        return LIVE_TRANSPORT_STATUS
    except Exception:
        return "not_configured"


def check_symbol_allowed(symbol: str, policy: dict | None = None) -> dict:
    """symbol이 live pilot 허용 종목인지 확인.

    Returns:
        {"allowed": bool, "blocks": list[str], "preferred": bool}
    """
    if policy is None:
        policy = compute_toss_live_pilot_policy()

    blocks: list[str] = []

    # 종목 제한 해제 — _BLOCKED_SYMBOLS 비어 있으면 차단 없음
    if symbol in _BLOCKED_SYMBOLS:
        blocks.append(f"blocked_symbol: {symbol}")

    preferred = symbol in _PREFERRED_SYMBOLS

    return {
        "allowed": len(blocks) == 0,
        "symbol": symbol,
        "blocks": blocks,
        "preferred": preferred,
    }
