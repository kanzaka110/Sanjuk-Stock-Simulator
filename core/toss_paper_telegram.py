"""
Toss paper 승인/취소 Telegram handler — 실제 주문 0건

callback data 파싱 → paper ledger 갱신 → 응답 텍스트 생성.
실제 Toss 주문 API 호출 없음. dry_run=True 강제.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ─── callback data prefix ────────────────────────────
# 형식: tp:<action>:<preview_id>:<symbol>
# action: a(approve), c(cancel), w(why)
CB_PREFIX = "tp:"


def build_callback_data(action: str, preview_id: str, symbol: str = "") -> str:
    """Telegram callback data 문자열 생성. 민감정보 미포함."""
    return f"{CB_PREFIX}{action}:{preview_id}:{symbol}"


def parse_callback_data(data: str) -> dict | None:
    """callback data 파싱. 잘못된 형식이면 None."""
    if not data or not data.startswith(CB_PREFIX):
        return None
    parts = data[len(CB_PREFIX):].split(":", 2)
    if len(parts) < 2:
        return None
    return {
        "action": parts[0],
        "preview_id": parts[1],
        "symbol": parts[2] if len(parts) > 2 else "",
    }


# ─── keyboard 생성 ───────────────────────────────────
def build_paper_preview_keyboard(
    preview_id: str,
    candidates: list[dict],
    cross_checks: list[dict],
) -> list[list[dict]]:
    """Telegram InlineKeyboard 버튼 배열 생성.

    반환: [[{text, callback_data}, ...], ...]
    """
    rows: list[list[dict]] = []

    for cand, cc in zip(candidates, cross_checks):
        symbol = cand.get("symbol", "")
        blocks = cc.get("blocks", [])

        if blocks:
            rows.append([
                {"text": f"차단 사유 · {symbol}", "callback_data": build_callback_data("w", preview_id, symbol)},
            ])
        else:
            rows.append([
                {"text": f"Paper 승인 · {symbol}", "callback_data": build_callback_data("a", preview_id, symbol)},
                {"text": f"Paper 취소 · {symbol}", "callback_data": build_callback_data("c", preview_id, symbol)},
            ])

    return rows


# ─── callback handler ────────────────────────────────
def handle_toss_paper_callback(callback_data: str) -> dict:
    """Telegram callback 처리. paper ledger만 변경. 실제 주문 없음.

    반환: {ok, action, message}
    """
    parsed = parse_callback_data(callback_data)
    if not parsed:
        return {
            "ok": False,
            "action": "unknown",
            "message": "⚠ 잘못된 요청입니다.\n실주문: 비활성",
        }

    action = parsed["action"]
    preview_id = parsed["preview_id"]
    symbol = parsed["symbol"] or None

    if action == "a":
        return _handle_approve(preview_id, symbol)
    elif action == "c":
        return _handle_cancel(preview_id, symbol)
    elif action == "w":
        return _handle_why(preview_id, symbol)
    else:
        return {
            "ok": False,
            "action": action,
            "message": "⚠ 알 수 없는 액션입니다.\n실주문: 비활성",
        }


def _handle_approve(preview_id: str, symbol: str | None) -> dict:
    """paper 승인 처리."""
    from core.toss_paper_ledger import approve_paper_order, format_approval_response

    result = approve_paper_order(preview_id, symbol)
    msg = format_approval_response(result)

    if not result.get("ok"):
        msg = f"⚠ Paper 승인 실패: {result.get('error', 'unknown')}\n실주문: 비활성"

    return {"ok": result.get("ok", False), "action": "approve", "message": msg}


def _handle_cancel(preview_id: str, symbol: str | None) -> dict:
    """paper 취소 처리."""
    from core.toss_paper_ledger import cancel_paper_order, format_cancel_response

    result = cancel_paper_order(preview_id, symbol)
    msg = format_cancel_response(result)

    return {"ok": result.get("ok", False), "action": "cancel", "message": msg}


def _handle_why(preview_id: str, symbol: str | None) -> dict:
    """차단 사유 조회."""
    from core.toss_paper_ledger import list_paper_orders

    orders = list_paper_orders()
    matched = [o for o in orders
               if o.get("preview_id") == preview_id
               and (symbol is None or o.get("symbol") == symbol)]

    if not matched:
        return {
            "ok": False,
            "action": "why",
            "message": "ℹ 해당 후보를 찾을 수 없습니다.\n실주문: 비활성",
        }

    import json
    lines = ["ℹ 차단/경고 사유"]
    for o in matched:
        blocks = json.loads(o.get("blocks", "[]"))
        warnings = json.loads(o.get("warnings", "[]"))
        lines.append(f"\n  {o['symbol']} ({o['status']})")
        if blocks:
            lines.append(f"  차단: {', '.join(blocks)}")
        if warnings:
            lines.append(f"  경고: {', '.join(warnings)}")
    lines.append("\n실주문: 비활성")
    return {"ok": True, "action": "why", "message": "\n".join(lines)}
