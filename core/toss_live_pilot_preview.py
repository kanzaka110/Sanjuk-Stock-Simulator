"""core/toss_live_pilot_preview.py

승인형 Live Pilot 미리보기 생성 (fail-closed, read-only intent).

이번 단계에서는 실제 주문 API를 호출하지 않는다.
모든 preview는 live_order_allowed=False 고정.
최종 2단계 승인 필요 경고 포함.

금지:
- 이 모듈에서 주문 API 호출
- live_order_allowed=True 반환
- 민감정보 출력
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _gen_preview_id() -> str:
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f")[:20]
    return f"tlive_{ts}"


def _get_live_pilot_policy() -> dict:
    try:
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        return compute_toss_live_pilot_policy()
    except Exception as e:
        log.warning("policy load failed: %s", e)
        return {
            "live_order_allowed": False,
            "max_order_krw": 100_000,
            "blocked_symbols": [],
            "sample_insufficient": True,
        }


def _check_symbol_blocks(symbol: str, policy: dict) -> list[str]:
    """symbol 차단 사유 목록 반환 (종목 제한 해제 시 빈 목록)."""
    blocks: list[str] = []
    blocked = policy.get("blocked_symbols", [])
    if symbol in blocked:
        blocks.append(f"blocked_symbol: {symbol}")
    return blocks


def _check_amount(estimated_krw: float, policy: dict) -> list[str]:
    """예상금액 한도 초과 여부 체크."""
    max_krw = policy.get("max_order_krw", 100_000)
    if estimated_krw > max_krw:
        return [f"금액_한도_초과: {estimated_krw:,.0f}원 > {max_krw:,.0f}원"]
    return []


def _check_price_source(candidate: dict) -> list[str]:
    """가격 source 불일치/없음 체크.

    candidate에 source_disagreement_pct 있으면 1% 초과 시 block.
    price 없으면 block.
    """
    blocks: list[str] = []
    price = candidate.get("limit_price") or candidate.get("price") or 0
    if not price or price <= 0:
        blocks.append("가격_없음_또는_장외")
    disagreement = candidate.get("source_disagreement_pct")
    if disagreement is not None and float(disagreement) > 1.0:
        blocks.append(f"source_불일치: {disagreement:.1f}%")
    return blocks


def build_live_pilot_preview(candidate: dict, policy: dict | None = None) -> dict:
    """승인형 Live Pilot 미리보기 생성.

    Args:
        candidate: {
            "symbol": str,
            "side": "buy" | "sell",
            "quantity": int,
            "limit_price": float,
            "source_disagreement_pct": float (optional),
            "currency": str (optional, default KRW),
        }
        policy: optional — 미지정 시 compute_toss_live_pilot_policy() 호출.

    Returns:
        preview dict — live_order_allowed 항상 False.
    """
    if policy is None:
        policy = _get_live_pilot_policy()
    preview_id = _gen_preview_id()

    symbol = candidate.get("symbol", "")
    side = candidate.get("side", "buy")
    quantity = int(candidate.get("quantity") or 0)
    limit_price = float(candidate.get("limit_price") or 0)
    estimated_krw = limit_price * quantity

    # 차단 체크 (순서대로)
    blocks: list[str] = []
    blocks += _check_symbol_blocks(symbol, policy)
    blocks += _check_price_source(candidate)
    if not blocks:  # 금액 체크는 기본 사항만 통과 시
        blocks += _check_amount(estimated_krw, policy)

    # 경고
    warnings: list[str] = list(policy.get("warnings", []))
    warnings.append("승인형 live pilot 준비 단계")
    warnings.append("아직 주문 전송 안 함")
    warnings.append("최종 2단계 승인 필요")

    ok = len(blocks) == 0

    preview: dict = {
        "ok": ok,
        "preview_id": preview_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "limit_price": limit_price,
        "estimated_amount_krw": estimated_krw,
        "live_pilot": True,
        "live_order_allowed": False,          # 이번 단계: 항상 False
        "live_order_sent": False,
        "adapter_status": "disabled",
        "requires_second_confirmation": True,
        "blocks": blocks,
        "warnings": warnings,
        "max_order_krw": policy.get("max_order_krw", 100_000),
        "sample_insufficient": policy.get("sample_insufficient", True),
    }

    if not ok:
        preview["block_summary"] = " · ".join(blocks)

    return preview


def build_live_pilot_telegram_text(preview: dict) -> str:
    """Telegram 전송용 Live Pilot 미리보기 텍스트 생성.

    금지 문구: 매수하기, 매도하기, 주문 실행, 실주문: 활성
    허용 문구: 실주문 미리보기, 최종 승인 필요, 아직 주문 전송 안 함
    """
    symbol = preview.get("symbol", "")
    side = preview.get("side", "buy")
    side_label = "매수 후보" if side == "buy" else "매도 후보"
    qty = preview.get("quantity", 0)
    price = preview.get("limit_price", 0)
    amount = preview.get("estimated_amount_krw", 0)
    max_krw = preview.get("max_order_krw", 100_000)
    blocks = preview.get("blocks", [])
    warnings = preview.get("warnings", [])

    lines = [
        "[승인형 Live Pilot 미리보기]",
        "실주문 미리보기 · 아직 주문 전송 안 함",
        "실주문: 비활성",
        "최종 2단계 승인 필요",
        "",
    ]

    if not preview.get("ok", False) or blocks:
        lines.append(f"⛔  {symbol} — 주문 차단")
        for b in blocks:
            lines.append(f"  · 차단: {b}")
        lines.append("주문 전송 비활성")
    else:
        lines.append(f"📋  {symbol}")
        lines.append(f"- 방향: {side_label}")
        lines.append(f"- 지정가: ₩{price:,.0f}")
        lines.append(f"- 수량: {qty}주")
        lines.append(f"- 예상금액: ₩{amount:,.0f}")
        lines.append(f"- 한도: 1회 최대 ₩{max_krw:,.0f}")
        lines.append("- 상태: 주문 API 호출 비활성")
        if warnings:
            lines.append("")
            for w in warnings[:3]:
                lines.append(f"  ℹ {w}")

    return "\n".join(lines)
