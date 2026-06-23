"""
Toss 자동거래 리스크 가드레일

모든 주문 후보를 평가하여 paper/live 허용 여부를 결정한다.
이번 단계에서 live는 항상 차단.
"""

from __future__ import annotations

from config import toss_automation as cfg


def evaluate_toss_order_candidate(
    candidate: dict,
    account_summary: dict,
    today_paper_stats: dict,
) -> dict:
    """주문 후보를 가드레일로 평가.

    반환: {allowed_for_paper, allowed_for_live, dry_run, reasons, max_order_krw}
    """
    reasons: list[str] = []
    paper_ok = True

    symbol = candidate.get("symbol", "")
    side = candidate.get("side", "")
    amount_krw = candidate.get("estimated_amount_krw", 0)
    confidence = candidate.get("confidence", 0)
    quote_age_sec = candidate.get("quote_age_sec", 9999)

    # ── 기본 플래그 ──
    if not cfg.TOSS_AUTOMATION_ENABLED:
        reasons.append("automation_disabled")

    if cfg.TOSS_KILL_SWITCH:
        reasons.append("kill_switch_on")

    if not cfg.TOSS_ALLOW_LIVE_ORDERS:
        reasons.append("live_orders_disabled")

    if cfg.TOSS_REQUIRE_TELEGRAM_APPROVAL:
        reasons.append("telegram_approval_required")

    if not cfg.TOSS_DRY_RUN:
        reasons.append("dry_run_off_blocked")

    # ── 종목 필터 ──
    if symbol in cfg.TOSS_SYMBOL_BLACKLIST:
        reasons.append("symbol_blacklisted")
        paper_ok = False

    if cfg.TOSS_SYMBOL_WHITELIST and symbol not in cfg.TOSS_SYMBOL_WHITELIST:
        reasons.append("symbol_not_in_whitelist")

    # ── 금액 한도 ──
    if amount_krw > cfg.TOSS_MAX_ORDER_KRW:
        reasons.append("max_order_exceeded")
        paper_ok = False

    daily_used = today_paper_stats.get("daily_amount_krw", 0)
    if daily_used + amount_krw > cfg.TOSS_MAX_DAILY_ORDER_KRW:
        reasons.append("daily_budget_exceeded")
        paper_ok = False

    # ── 현금 버퍼 ──
    cash_krw = account_summary.get("cash", {}).get("krw", 0)
    if side == "buy" and cash_krw - amount_krw < cfg.TOSS_MIN_CASH_BUFFER_KRW:
        reasons.append("cash_buffer_breach")
        paper_ok = False

    # ── 포지션 ──
    holdings_count = account_summary.get("holdings_count", 0)
    if side == "buy" and holdings_count >= cfg.TOSS_MAX_POSITIONS:
        reasons.append("max_positions_reached")
        paper_ok = False

    # ── 시세 신선도 ──
    if quote_age_sec > cfg.TOSS_MAX_QUOTE_AGE_SEC:
        reasons.append("quote_stale")
        paper_ok = False

    # ── 신뢰도 ──
    if confidence < cfg.TOSS_MIN_CONFIDENCE:
        reasons.append("low_confidence")
        paper_ok = False

    # live는 이번 단계에서 항상 불가
    live_ok = False

    return {
        "allowed_for_paper": paper_ok,
        "allowed_for_live": live_ok,
        "dry_run": True,
        "reasons": reasons,
        "max_order_krw": cfg.TOSS_MAX_ORDER_KRW,
    }
