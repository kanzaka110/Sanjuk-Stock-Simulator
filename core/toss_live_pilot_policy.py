"""core/toss_live_pilot_policy.py

승인형 Toss Live Pilot 정책 모듈 (read-only, fail-closed).

활성화 조건 (3개 env 모두 true여야):
  TOSS_LIVE_PILOT_ENABLED=true
  TOSS_LIVE_ORDER_ALLOWED=true
  TOSS_LIVE_ADAPTER_ENABLED=true

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
# 최종 정책: 1회 한도 50만원, 1일 상한(cap) 200만원, 주문 건수 제한 없음.
# 200만원은 목표금액이 아니라 최대 상한이다. 좋은 후보가 없으면 매수 0원/HOLD가 정상.
_SAMPLE_LIVE_THRESHOLD = 5          # evaluated_count < 5 → 표본부족 경고(한도는 동일)
_MAX_ORDER_KRW = 500_000            # 1회 주문 한도
_MAX_DAILY_KRW = 2_000_000          # 1일 주문 총액 상한(cap, 목표 아님)
_MAX_ORDERS_PER_DAY = None          # 주문 건수 제한 없음 (총액 cap으로만 관리)

# 종목 제한 해제: 블록목록/허용목록 모두 비활성 (BUY_ONLY/Hermes PASS/최종승인/금액한도 가드는 유지)
_BLOCKED_SYMBOLS: frozenset[str] = frozenset()

# 허용 종목 화이트리스트 해제 (빈 목록 = 종목 화이트리스트 강제 없음)
_LIVE_ALLOWED_SYMBOLS: list[str] = []

# 고신뢰 종목 우선 (paper 참조용)
_PREFERRED_SYMBOLS: list[str] = ["069500.KS"]

_ALLOWED_ASSET_TYPES: list[str] = ["KR_ETF", "KR_STOCK", "US_STOCK"]

# BUY_ONLY 정책 (이번 단계 고정, env로 제어 안 함)
_LIVE_PILOT_SIDE_MODE: str = "BUY_ONLY"
_LIVE_PILOT_ALLOWED_SIDES: list[str] = ["buy"]
_LIVE_PILOT_BLOCK_SELL: bool = True


# ── 내부 helpers ───────────────────────────────────────────────────

def _is_live_pilot_env_enabled() -> bool:
    """TOSS_LIVE_PILOT_ENABLED=true 환경변수 확인."""
    return os.environ.get("TOSS_LIVE_PILOT_ENABLED", "").strip().lower() == "true"


def _is_live_order_allowed_env() -> bool:
    """TOSS_LIVE_ORDER_ALLOWED=true 환경변수 확인."""
    return os.environ.get("TOSS_LIVE_ORDER_ALLOWED", "").strip().lower() == "true"


def _is_live_adapter_enabled_env() -> bool:
    """TOSS_LIVE_ADAPTER_ENABLED=true 환경변수 확인."""
    return os.environ.get("TOSS_LIVE_ADAPTER_ENABLED", "").strip().lower() == "true"


def _all_live_gates_open() -> bool:
    """3개 env gate 모두 true여야 adapter enabled 가능."""
    return (
        _is_live_pilot_env_enabled()
        and _is_live_order_allowed_env()
        and _is_live_adapter_enabled_env()
    )


def _get_evaluated_count() -> int:
    """Toss Paper evaluated_count 조회 (오류 시 0 반환)."""
    try:
        from core.toss_paper_performance import get_paper_performance_summary
        s = get_paper_performance_summary().get("summary", {})
        return int(s.get("evaluated_count", 0))
    except Exception as e:
        log.debug("evaluated_count 조회 실패: %s", e)
        return 0


# ── 공개 API ───────────────────────────────────────────────────────

def compute_toss_live_pilot_policy(
    evaluated_count: int | None = None,
) -> dict:
    """승인형 live pilot 정책 계산.

    3개 env gate(TOSS_LIVE_PILOT_ENABLED + TOSS_LIVE_ORDER_ALLOWED + TOSS_LIVE_ADAPTER_ENABLED)
    모두 true일 때만 adapter_status=enabled, live_order_allowed=True.
    그 외에는 기본값 disabled.

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

    # 한도는 표본 충분 여부와 무관하게 고정 (1회 50만원). 표본부족이면 경고만 추가.
    max_order_krw = _MAX_ORDER_KRW
    if insufficient:
        warnings: list[str] = [
            "Paper 표본부족 — 수동 최종 승인 필수 (좋은 후보 있을 때만 매수)"
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

    policy: dict = {
        "mode": "approval_only_live_pilot",
        "live_pilot_enabled": live_pilot_enabled,
        "live_order_allowed": live_order_allowed,
        "adapter_status": adapter_status,
        "requires_user_confirmation": True,
        "requires_second_confirmation": True,
        # env 상태 (개별)
        "env_live_pilot_enabled": env_pilot,
        "env_live_order_allowed": env_order,
        "env_live_adapter_enabled": env_adapter,
        "all_live_gates_open": all_gates,
        # 한도 (1일 상한은 cap — 목표금액 아님)
        "max_order_krw": max_order_krw,
        "max_daily_krw": _MAX_DAILY_KRW,
        "daily_krw_is_cap": True,
        "daily_krw_is_target": False,
        "max_orders_per_day": _MAX_ORDERS_PER_DAY,  # None = 건수 제한 없음
        "max_orders_per_day_label": "unlimited",
        "order_count_limited": False,
        "daily_policy_note": (
            "1일 상한 ₩2,000,000 (목표 아님). 좋은 후보가 있을 때만 매수하며, "
            "후보가 없으면 매수 없음/HOLD가 정상이다. 상한 금액을 억지로 맞추지 않는다."
        ),
        # 종목
        "allowed_asset_types": _ALLOWED_ASSET_TYPES,
        "blocked_symbols": sorted(_BLOCKED_SYMBOLS),
        "live_allowed_symbols": _LIVE_ALLOWED_SYMBOLS,
        "preferred_symbols": _PREFERRED_SYMBOLS,
        # 상태
        "evaluated_count": evaluated_count,
        "sample_insufficient": insufficient,
        "warnings": warnings,
        "block_reason": block_reason,
        "reason": "승인형 live pilot — 수동 최종 승인 전용 (기본: 비활성)",
        # BUY_ONLY 정책 (고정)
        "side_mode": _LIVE_PILOT_SIDE_MODE,
        "allowed_sides": list(_LIVE_PILOT_ALLOWED_SIDES),
        "sell_allowed": False,
        # transport 상태
        "live_transport_status": _get_live_transport_status(),
    }

    return policy


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
