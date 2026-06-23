"""
Toss/KIS/기존 데이터 교차 검증 — read-only

KIS 시세, Toss 계좌/현금/장 시간, 기존 신뢰도/성과 DB를 비교하여
판단 품질 신호를 생성한다. 실제 주문 없음.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def cross_check_candidate(
    symbol: str,
    side: str,
    estimated_amount_krw: float,
    toss_context: dict,
) -> dict:
    """후보를 Toss/KIS/기존 데이터로 교차 검증.

    반환: {score_adjustments, blocks, warnings, toss_readiness, live_order_allowed}
    """
    adjustments: list[dict] = []
    blocks: list[str] = []
    warnings: list[str] = []

    from config import toss_automation as cfg

    cash_krw = toss_context.get("cash_krw", 0)
    holdings_count = toss_context.get("holdings_count", 0)
    usdkrw = toss_context.get("usdkrw", 0)
    dq = toss_context.get("data_quality", {})

    # ── 1. 현금 충분성 ──
    if side == "buy" and estimated_amount_krw > cash_krw:
        blocks.append("cash_insufficient")
        adjustments.append({"factor": "cash_insufficient", "delta": -30})

    # ── 2. 현금 버퍼 침해 ──
    if side == "buy" and cash_krw - estimated_amount_krw < cfg.TOSS_MIN_CASH_BUFFER_KRW:
        blocks.append("cash_buffer_breach")
        adjustments.append({"factor": "cash_buffer_breach", "delta": -20})

    # ── 3. 환율 stale ──
    if not dq.get("fx_available"):
        warnings.append("fx_unavailable")
        adjustments.append({"factor": "fx_unavailable", "delta": -5})

    # ── 4. Toss 보유종목 중복 ──
    toss_holdings = toss_context.get("holdings", [])
    held_symbols = {h.get("symbol", "") for h in toss_holdings if isinstance(h, dict)}
    if symbol in held_symbols:
        warnings.append("already_held_in_toss")
        adjustments.append({"factor": "already_held_in_toss", "delta": -10})

    # ── 5. paper ledger 중복 ──
    try:
        from core.toss_paper_trading import list_paper_trades
        recent = list_paper_trades(limit=20, today_only=True)
        paper_symbols = {t.get("symbol", "") for t in recent
                         if t.get("guard_status") in ("paper_filled", "allowed")}
        if symbol in paper_symbols:
            warnings.append("already_in_paper_today")
            adjustments.append({"factor": "paper_duplicate", "delta": -5})
    except Exception:
        pass

    # ── 6. 블랙리스트 ──
    if symbol in cfg.TOSS_SYMBOL_BLACKLIST:
        blocks.append("symbol_blacklisted")
        adjustments.append({"factor": "blacklisted", "delta": -50})

    # ── 7. MU 보호 (기존 8주 보유, 별도 승인 없이 Toss 제외) ──
    if symbol == "MU":
        blocks.append("mu_protected")
        warnings.append("MU는 기존 삼성증권 보유 · Toss 자동 후보 제외")

    # ── 8. 최대 포지션 ──
    if side == "buy" and holdings_count >= cfg.TOSS_MAX_POSITIONS:
        blocks.append("max_positions_reached")

    # ── 9. 1회 한도 ──
    if estimated_amount_krw > cfg.TOSS_MAX_ORDER_KRW:
        blocks.append("max_order_exceeded")

    # ── 10. Toss 데이터 가용성 ──
    if not toss_context.get("enabled"):
        warnings.append("toss_not_configured")

    # ── 11. Paper 성과 policy — consensus_anomaly 차단 ──
    try:
        from core.toss_paper_policy import compute_toss_paper_policy, apply_toss_paper_policy_to_candidate
        policy = compute_toss_paper_policy()
        cand_stub = {"symbol": symbol, "side": side, "limit_price": 0, "quantity": 0,
                     "estimated_amount_krw": estimated_amount_krw}
        applied = apply_toss_paper_policy_to_candidate(cand_stub, policy)
        pp = applied.get("paper_policy", {})
        for b in pp.get("blocks", []):
            if b not in blocks:
                blocks.append(b)
                adjustments.append({"factor": b, "delta": -50})
        for w in pp.get("warnings", []):
            if w not in warnings:
                warnings.append(w)
    except Exception as exc:
        logger.debug("paper policy 적용 실패: %s", exc)

    readiness = "paper_only"
    if blocks:
        readiness = "blocked"

    return {
        "score_adjustments": adjustments,
        "blocks": blocks,
        "warnings": warnings,
        "toss_readiness": readiness,
        "live_order_allowed": False,
    }


def cross_check_summary(toss_context: dict) -> dict:
    """전반적인 Toss/KIS 교차 검증 요약 (dashboard용)."""
    dq = toss_context.get("data_quality", {})
    auto = toss_context.get("automation", {})

    checks = []
    checks.append({
        "name": "Toss API 연결",
        "status": "OK" if dq.get("toss_available") else "FAIL",
        "ok": dq.get("toss_available", False),
    })
    checks.append({
        "name": "현금/예수금",
        "status": "OK" if dq.get("cash_available") else "FAIL",
        "ok": dq.get("cash_available", False),
    })
    checks.append({
        "name": "USD/KRW 환율",
        "status": "OK" if dq.get("fx_available") else "FAIL",
        "ok": dq.get("fx_available", False),
    })
    checks.append({
        "name": "장 캘린더",
        "status": "OK" if dq.get("calendar_available") else "FAIL",
        "ok": dq.get("calendar_available", False),
    })
    checks.append({
        "name": "실주문 차단",
        "status": "ON" if not auto.get("live_orders_allowed", False) else "OFF",
        "ok": not auto.get("live_orders_allowed", False),
    })

    warnings = dq.get("warnings", [])

    return {
        "checks": checks,
        "all_ok": all(c["ok"] for c in checks),
        "warnings": warnings,
        "toss_readiness": "paper_only" if dq.get("toss_available") else "unavailable",
        "live_order_allowed": False,
    }
