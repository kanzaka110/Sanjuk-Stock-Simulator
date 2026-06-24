"""core/toss_live_pilot_policy.py

승인형 Toss Live Pilot 정책 모듈 (read-only, fail-closed).

이번 단계에서는 실제 주문 API 호출을 하지 않는다.
env TOSS_LIVE_PILOT_ENABLED=true 가 있어도 adapter는 항상 blocked.
실제 주문 연결은 별도 승인 단계에서만 가능.

금지:
- 이 모듈에서 주문 API 직접 호출 금지
- live_order_allowed=True 반환 금지 (이번 단계)
- 민감정보(key/secret/accountNo) 출력 금지
"""

from __future__ import annotations

import os
import logging

log = logging.getLogger(__name__)

# ── 정책 상수 ──────────────────────────────────────────────────────
_SAMPLE_LIVE_THRESHOLD = 5          # evaluated_count < 5 → 초보수 모드
_MAX_ORDER_KRW_INSUFFICIENT = 100_000
_MAX_ORDER_KRW_STABLE = 300_000
_MAX_DAILY_KRW = 300_000
_MAX_ORDERS_PER_DAY = 1

# 위험/anomaly 이력 종목 → 항상 block
_BLOCKED_SYMBOLS: frozenset[str] = frozenset(["161510.KS", "005930.KS"])

# 고신뢰 종목 우선
_PREFERRED_SYMBOLS: list[str] = ["069500.KS"]

_ALLOWED_ASSET_TYPES: list[str] = ["KR_ETF", "KR_STOCK", "US_STOCK"]


# ── 내부 helpers ───────────────────────────────────────────────────

def _is_live_pilot_env_enabled() -> bool:
    """TOSS_LIVE_PILOT_ENABLED=true 환경변수 존재 여부만 확인."""
    return os.environ.get("TOSS_LIVE_PILOT_ENABLED", "").strip().lower() == "true"


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

    Returns:
        policy dict — live_order_allowed는 이번 단계에서 항상 False.
    """
    if evaluated_count is None:
        evaluated_count = _get_evaluated_count()

    env_enabled = _is_live_pilot_env_enabled()
    insufficient = evaluated_count < _SAMPLE_LIVE_THRESHOLD

    # 예산/한도 결정
    if insufficient:
        max_order_krw = _MAX_ORDER_KRW_INSUFFICIENT
        warnings: list[str] = ["Paper 표본부족 — live pilot은 초소액/수동 승인만"]
    else:
        max_order_krw = _MAX_ORDER_KRW_STABLE
        warnings = []

    policy: dict = {
        "mode": "approval_only_live_pilot",
        # 이번 단계: 항상 False — adapter가 disabled
        "live_pilot_enabled": False,
        "live_order_allowed": False,
        "adapter_status": "disabled",
        "requires_user_confirmation": True,
        "requires_second_confirmation": True,
        "env_live_pilot_enabled": env_enabled,
        "max_order_krw": max_order_krw,
        "max_daily_krw": _MAX_DAILY_KRW,
        "max_orders_per_day": _MAX_ORDERS_PER_DAY,
        "allowed_asset_types": _ALLOWED_ASSET_TYPES,
        "blocked_symbols": sorted(_BLOCKED_SYMBOLS),
        "preferred_symbols": _PREFERRED_SYMBOLS,
        "evaluated_count": evaluated_count,
        "sample_insufficient": insufficient,
        "warnings": warnings,
        "reason": "승인형 live pilot 준비 단계 — 실제 주문 호출 비활성",
    }

    if not env_enabled:
        policy["block_reason"] = "TOSS_LIVE_PILOT_ENABLED env not set"

    return policy


def check_symbol_allowed(symbol: str, policy: dict | None = None) -> dict:
    """symbol이 live pilot 허용 종목인지 확인.

    Returns:
        {"allowed": bool, "blocks": list[str], "preferred": bool}
    """
    if policy is None:
        policy = compute_toss_live_pilot_policy()

    blocks: list[str] = []

    if symbol in _BLOCKED_SYMBOLS:
        if symbol == "161510.KS":
            blocks.append("위험_저신뢰_종목")
        elif symbol == "005930.KS":
            blocks.append("price_anomaly_history")
        else:
            blocks.append("blocked_symbol")

    preferred = symbol in _PREFERRED_SYMBOLS

    return {
        "allowed": len(blocks) == 0,
        "symbol": symbol,
        "blocks": blocks,
        "preferred": preferred,
    }
