"""
Toss paper 주문표 미리보기 생성 — Telegram용

브리핑 후보 → 교차 검증 결과를 바탕으로 사람이 읽기 쉬운 주문표 텍스트를 만든다.
실제 주문 아님. paper/dry-run 기록만 가능.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def generate_preview_id() -> str:
    """안전한 preview ID 생성. 민감정보 미포함."""
    now = datetime.now(KST)
    return f"tosspaper_{now.strftime('%Y%m%d_%H%M%S')}_{now.microsecond // 1000:03d}"


def build_toss_paper_order_preview(
    candidates: list[dict],
    toss_context: dict,
    cross_checks: list[dict],
) -> str:
    """Telegram용 주문표 미리보기 텍스트 생성.

    Args:
        candidates: [{symbol, side, quantity, limit_price, estimated_amount_krw, ...}, ...]
        toss_context: get_toss_decision_context() 결과
        cross_checks: [cross_check_candidate() 결과, ...] (candidates와 1:1 대응)

    Returns:
        Telegram 메시지용 텍스트
    """
    if not candidates:
        return _empty_preview(toss_context)

    preview_id = generate_preview_id()
    cash_krw = toss_context.get("cash_krw", 0)
    auto = toss_context.get("automation", {})

    lines = [
        "📋 [Toss Paper 주문표 미리보기 · 실제 주문 아님]",
        "",
        f"preview: {preview_id} (실제 주문 ID 아님)",
        f"Toss 현금: ₩{cash_krw:,.0f}",
        "실주문: 비활성",
        f"모드: {auto.get('mode', 'paper')} / dry_run={auto.get('dry_run', True)}",
        "",
    ]

    for i, (cand, cc) in enumerate(zip(candidates, cross_checks), 1):
        lines.append(_render_candidate(i, cand, cc, toss_context))
        lines.append("")

    lines.extend([
        "─" * 30,
        "⚠ 승인해도 paper 기록만 가능",
        "⚠ 실주문 비활성 · Telegram 승인 전 실제 주문 불가",
        "⚠ 기존 삼성증권/수동 포트폴리오 미합산",
    ])

    return "\n".join(lines)


def _render_candidate(idx: int, cand: dict, cc: dict, ctx: dict) -> str:
    """후보 1건 렌더링."""
    symbol = cand.get("symbol", "?")
    side = cand.get("side", "?")
    quantity = cand.get("quantity", 0)
    limit_price = cand.get("limit_price", 0)
    amount = cand.get("estimated_amount_krw", 0)
    confidence = cand.get("confidence", 0)
    reason = cand.get("reason", "")

    blocks = cc.get("blocks", [])
    warnings = cc.get("warnings", [])
    readiness = cc.get("toss_readiness", "unknown")
    live = cc.get("live_order_allowed", False)

    is_blocked = bool(blocks)
    label = "차단 후보" if is_blocked else "paper 매수 후보" if side == "buy" else "paper 매도 후보"

    cash_krw = ctx.get("cash_krw", 0)
    buffer_ok = cash_krw - amount >= 2_000_000 if side == "buy" else True

    lines = [f"{'❌' if is_blocked else '📌'} {idx}. {symbol}"]
    lines.append(f"  구분: {label}")
    lines.append(f"  계좌: Toss 실전 AI 자동거래 계좌")

    # paper policy sizing 텍스트
    _policy_text: str | None = None
    try:
        from core.toss_paper_policy import compute_toss_paper_policy, apply_toss_paper_policy_to_candidate, get_policy_sizing_text
        _policy = compute_toss_paper_policy()
        _applied = apply_toss_paper_policy_to_candidate(cand, _policy)
        _policy_text = get_policy_sizing_text(_policy, _applied)
    except Exception:
        pass

    price_currency = cand.get("_price_currency", "KRW")
    limit_price_usd = cand.get("_limit_price_usd")
    cand_usdkrw = cand.get("_usdkrw")

    if not is_blocked:
        lines.append(f"  방향: paper {side}")
        if price_currency == "USD" and limit_price_usd is not None:
            lines.append(f"  지정가: ${limit_price_usd:,.2f}")
            if cand_usdkrw:
                lines.append(f"  환율: {cand_usdkrw:,.0f}원")
        else:
            lines.append(f"  지정가: ₩{limit_price:,.0f}")
        lines.append(f"  수량: {quantity}주")
        lines.append(f"  예상금액: ₩{amount:,.0f}")
        lines.append(f"  Toss 현금: ₩{cash_krw:,.0f}")
        lines.append(f"  현금 버퍼: {'OK' if buffer_ok else '⚠ 침해'}")
        lines.append(f"  신뢰도: {confidence:.0%}")
        if reason:
            lines.append(f"  사유: {reason}")
        lines.append(f"  교차검증: {readiness}")
        if _policy_text:
            lines.append(f"  정책: {_policy_text}")
        lines.append("  실주문: 비활성")
        lines.append(f"  액션: 승인해도 paper 기록만 가능")
    else:
        lines.append(f"  차단 사유: {', '.join(blocks)}")
        if warnings:
            lines.append(f"  경고: {', '.join(warnings)}")
        if _policy_text:
            lines.append(f"  정책: {_policy_text}")
        lines.append("  실주문: 비활성")
        lines.append(f"  액션: 대기")

    return "\n".join(lines)


def _empty_preview(ctx: dict) -> str:
    """후보 0건일 때."""
    cash = ctx.get("cash_krw", 0)
    return (
        "📋 [Toss Paper 주문표 미리보기 · 실제 주문 아님]\n\n"
        "Toss paper 후보 없음\n"
        f"Toss 현금: ₩{cash:,.0f}\n"
        "실주문: 비활성\n"
        "승인 대상 없음"
    )
