"""core/toss_live_pilot_hermes_bridge.py

Hermes 검증 미러링 브릿지 v1.

[구조]
- format_hermes_live_pilot_verify_message(): Hermes가 볼 검증 요청 메시지 생성
- maybe_send_hermes_verification_request(): mirror env ON이면 Hermes 채널로 전송
- build_default_hermes_verdict(): 자동 판정 helper (PASS/HOLD/BLOCK)
- get_mirror_status(): 미러 설정 상태 조회 (read-only)

[env]
  HERMES_VERIFY_MIRROR_ENABLED=false  # 기본 OFF
  HERMES_VERIFY_CHAT_ID=              # Hermes 전문방 채팅 ID
  HERMES_VERIFY_THREAD_ID=            # 스레드 ID (선택)

[금지]
- 실제 주문 API 호출 금지
- live_order_allowed=True 반환 금지
- 토큰/secret/chat_id 로그 평문 출력 금지
- POST/PUT/DELETE/PATCH web route 금지
- .env 수정 금지
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_FORBIDDEN_CTA = frozenset([
    "자동매매 시작",
    "자동거래 시작",
    "실주문: 활성",
    "주문 실행",
    "매수하기",
    "매도하기",
])


# ─── env 설정 ────────────────────────────────────────────

def _get_mirror_config() -> dict:
    """env에서 미러 설정 읽기. secret/chat_id 값 로그 금지."""
    enabled_str = os.environ.get("HERMES_VERIFY_MIRROR_ENABLED", "false").strip().lower()
    enabled = enabled_str in ("1", "true", "yes", "on")
    chat_id = os.environ.get("HERMES_VERIFY_CHAT_ID", "").strip()
    thread_id = os.environ.get("HERMES_VERIFY_THREAD_ID", "").strip()
    return {
        "enabled": enabled,
        "chat_configured": bool(chat_id),
        "thread_configured": bool(thread_id),
        # chat_id / thread_id 값 자체는 이 dict에 포함하지 않음 (로그 방지)
        "_chat_id": chat_id,
        "_thread_id": thread_id,
    }


def get_mirror_status() -> dict:
    """미러 설정 상태 반환 (read-only, 비밀 미포함)."""
    cfg = _get_mirror_config()
    return {
        "mirror_enabled": cfg["enabled"],
        "mirror_target_configured": cfg["chat_configured"],
    }


# ─── 검증 요청 메시지 포맷 ───────────────────────────────

def format_hermes_live_pilot_verify_message(context: dict) -> str:
    """Hermes 검증 요청 Telegram 메시지 생성.

    human-readable 요약 + 기계 파싱 [HERMES_LIVE_PILOT_VERIFY] 블록.
    민감정보 없음, 금지 CTA 없음.

    Args:
        context: build_mirror_context() 또는 build_hermes_verification_context()가 반환하는 dict

    Returns:
        str — 전체 Telegram 메시지
    """
    symbol = context.get("symbol", "")
    side = context.get("side", "buy")
    quantity = context.get("quantity", 0)
    limit_price = float(context.get("limit_price") or 0)
    estimated = float(context.get("estimated_amount_krw") or 0)
    max_krw = float(context.get("max_order_krw") or 100_000)
    max_daily = float(context.get("max_daily_krw") or 300_000)
    side_mode = context.get("side_mode", "BUY_ONLY")
    adapter_status = context.get("adapter_status", "disabled")
    live_transport_status = context.get("live_transport_status", "not_configured")
    live_order_allowed = context.get("live_order_allowed", False)
    sell_allowed = context.get("sell_allowed", False)
    verification_id = context.get("verification_id", "")
    pilot_id = context.get("pilot_id", "")
    preview_id = context.get("preview_id", "")
    blocked_symbols = context.get("blocked_symbols", "")
    allowed_symbols = context.get("allowed_symbols", "")
    paper_count = context.get("paper_evaluated_count", 0)
    sample_status = context.get("sample_status", "insufficient")
    expires_in = context.get("expires_in_minutes", 10)

    price_str = f"₩{limit_price:,.0f}" if limit_price > 0 else "미확인"
    amount_str = f"₩{estimated:,.0f}" if estimated > 0 else "미확인"

    lines = [
        "[Hermes 교차검증 요청 · Toss BUY_ONLY Live Pilot]",
        "상태: Hermes 검증 대기",
        "실주문: 비활성",
        "아직 주문 전송 안 함",
        "",
        "요약:",
        f"- 종목: {symbol}",
        f"- 방향: {side}",
        f"- 수량: {quantity}",
        f"- 지정가: {price_str}",
        f"- 예상금액: {amount_str}",
        f"- 한도: 1회 ₩{max_krw:,.0f} / 1일 ₩{max_daily:,.0f}",
        f"- side_mode: {side_mode}",
        f"- live_order_allowed: {str(live_order_allowed).lower()}",
        f"- adapter_status: {adapter_status}",
        f"- live_transport_status: {live_transport_status}",
        "",
        "[HERMES_LIVE_PILOT_VERIFY]",
        f"verification_id: {verification_id}",
        f"pilot_id: {pilot_id}",
        f"preview_id: {preview_id}",
        f"symbol: {symbol}",
        f"side: {side}",
        f"quantity: {quantity}",
        f"limit_price: {limit_price}",
        f"estimated_amount_krw: {estimated}",
        f"max_order_krw: {max_krw:.0f}",
        f"max_daily_krw: {max_daily:.0f}",
        f"side_mode: {side_mode}",
        f"sell_allowed: {str(sell_allowed).lower()}",
        f"live_order_allowed: {str(live_order_allowed).lower()}",
        f"adapter_status: {adapter_status}",
        f"live_transport_status: {live_transport_status}",
        f"paper_evaluated_count: {paper_count}",
        f"sample_status: {sample_status}",
        f"blocked_symbols: {blocked_symbols}",
        f"allowed_symbols: {allowed_symbols}",
        "hermes_required: true",
        f"expires_in_minutes: {expires_in}",
        "[/HERMES_LIVE_PILOT_VERIFY]",
        "",
        "Hermes 응답 기대:",
        "PASS / HOLD / BLOCK 중 하나.",
        "PASS가 없으면 최종승인 버튼은 차단됩니다.",
    ]

    msg = "\n".join(lines)

    # 금지 CTA 검사
    for forbidden in _FORBIDDEN_CTA:
        if forbidden in msg:
            log.error("금지 CTA 감지: %s", forbidden)
            raise ValueError(f"금지 CTA 포함: {forbidden!r}")

    # 민감정보 검사
    for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
        if kw in msg:
            log.error("민감정보 감지: %s", kw)
            raise ValueError(f"민감정보 포함: {kw!r}")

    return msg


# ─── 미러 전송 ────────────────────────────────────────────

def maybe_send_hermes_verification_request(
    preview_record: dict,
    verification: dict,
    policy: dict,
) -> dict:
    """Hermes 전문방으로 검증 요청 미러링.

    env disabled → {"ok": False, "skipped": True, "reason": "mirror_disabled"}
    target missing → {"ok": False, "skipped": True, "reason": "mirror_target_missing"}
    enabled → Telegram 전송 시도

    전송 실패해도 live pilot preview / verification 자체는 영향 없음.
    토큰/chat_id 로그 출력 금지.

    Returns:
        {"ok": bool, "skipped": bool, "reason"?: str, "sent"?: bool, "verification_id"?: str}
    """
    cfg = _get_mirror_config()

    if not cfg["enabled"]:
        return {"ok": False, "skipped": True, "reason": "mirror_disabled"}

    if not cfg["chat_configured"]:
        return {"ok": False, "skipped": True, "reason": "mirror_target_missing"}

    context = _build_mirror_context(preview_record, verification, policy)

    try:
        msg = format_hermes_live_pilot_verify_message(context)
    except Exception as e:
        log.error("Hermes mirror message build failed: %s", e)
        return {
            "ok": False,
            "skipped": False,
            "reason": f"message_build_failed: {e}",
            "verification_id": verification.get("verification_id", ""),
        }

    sent = _send_to_hermes_channel(msg, cfg)

    vid = verification.get("verification_id", "")
    if sent:
        log.info("Hermes verification mirror sent: verification_id=%s", vid)
        return {"ok": True, "skipped": False, "sent": True, "verification_id": vid}
    else:
        return {
            "ok": False,
            "skipped": False,
            "reason": "telegram_send_failed",
            "verification_id": vid,
        }


def _build_mirror_context(
    preview_record: dict,
    verification: dict,
    policy: dict,
) -> dict:
    """Hermes 메시지용 컨텍스트 dict 빌드."""
    blocked = policy.get("blocked_symbols") or []
    allowed = [s for s in ["091180.KS", "360750.KS"] if s not in blocked]
    return {
        "verification_id": verification.get("verification_id", ""),
        "pilot_id": verification.get("pilot_id", preview_record.get("pilot_id", "")),
        "preview_id": verification.get("preview_id", preview_record.get("preview_id", "")),
        "symbol": preview_record.get("symbol", ""),
        "side": preview_record.get("side", "buy"),
        "quantity": preview_record.get("quantity", 0),
        "limit_price": float(preview_record.get("limit_price") or 0),
        "estimated_amount_krw": float(preview_record.get("estimated_amount_krw") or 0),
        "max_order_krw": policy.get("max_order_krw", 100_000),
        "max_daily_krw": policy.get("max_daily_krw", 300_000),
        "side_mode": policy.get("side_mode", "BUY_ONLY"),
        "sell_allowed": policy.get("sell_allowed", False),
        "live_order_allowed": policy.get("live_order_allowed", False),
        "adapter_status": policy.get("adapter_status", "disabled"),
        "live_transport_status": policy.get("live_transport_status", "not_configured"),
        "paper_evaluated_count": 0,
        "sample_status": "insufficient",
        "blocked_symbols": ",".join(str(s) for s in blocked),
        "allowed_symbols": ",".join(allowed),
        "expires_in_minutes": 10,
    }


def _send_to_hermes_channel(text: str, cfg: dict) -> bool:
    """Hermes 전문방으로 Telegram 메시지 전송.

    token / chat_id 값을 로그에 평문 출력하지 않음.
    """
    import requests as req

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = cfg.get("_chat_id", "")
    thread_id = cfg.get("_thread_id", "")

    if not token or not chat_id:
        log.warning("Hermes 미러 Telegram 미설정 (token 또는 chat_id 없음)")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            pass

    try:
        res = req.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            return True
        log.warning("Hermes mirror 전송 실패: HTTP %d", res.status_code)
        return False
    except Exception as e:
        log.error("Hermes mirror 전송 오류: %s", e)
        return False


# ─── 자동 판정 helper ─────────────────────────────────────

def build_default_hermes_verdict(context: dict) -> dict:
    """Hermes 기본 자동 판정 helper.

    이번 단계: 자동 PASS 남발 금지.

    판정 우선순위:
      1. sell → BLOCK
      2. blocked_symbol → BLOCK
      3. amount > max_order_krw → BLOCK
      4. price=0 → HOLD
      5. valid buy + transport not_configured + adapter disabled → PASS (execution_blocked=true)
      6. 기타 → HOLD

    중요:
      Hermes PASS는 "사용자 최종승인 검증 통과"이지 env/adapter 활성화 스위치가 아님.
      실제 주문 가능 여부는 stock-bot gate가 따로 판단.

    Returns:
        {"status": str, "reasons": list, "checks": dict}
    """
    symbol = context.get("symbol", "")
    side = context.get("side", "buy")
    limit_price = float(context.get("limit_price") or 0)
    estimated = float(context.get("estimated_amount_krw") or 0)
    max_krw = float(context.get("max_order_krw") or 100_000)
    live_transport_status = context.get("live_transport_status", "not_configured")
    live_order_allowed = context.get("live_order_allowed", False)
    adapter_status = context.get("adapter_status", "disabled")

    blocked_symbols_raw = context.get("blocked_symbols", "")
    if isinstance(blocked_symbols_raw, list):
        blocked_set = set(blocked_symbols_raw)
    else:
        blocked_set = {s.strip() for s in str(blocked_symbols_raw).split(",") if s.strip()}

    # 1. sell → BLOCK
    if side == "sell":
        return {
            "status": "BLOCK",
            "reasons": ["sell_not_allowed_in_buy_only_pilot"],
            "checks": {"sell_guard": "FAIL"},
        }

    # 2. blocked symbol → BLOCK
    if symbol in blocked_set:
        return {
            "status": "BLOCK",
            "reasons": [f"blocked_symbol: {symbol}"],
            "checks": {"symbol_guard": f"FAIL: {symbol}"},
        }

    # 3. amount exceed → BLOCK
    if estimated > max_krw:
        return {
            "status": "BLOCK",
            "reasons": [f"amount_over_limit: {estimated:,.0f} > {max_krw:,.0f}"],
            "checks": {"amount_guard": f"FAIL: {estimated:,.0f} > {max_krw:,.0f}"},
        }

    # 4. price missing → HOLD
    if limit_price <= 0:
        return {
            "status": "HOLD",
            "reasons": ["price_missing_or_zero"],
            "checks": {"price_guard": "HOLD: price=0"},
        }

    # 5. valid buy + transport not_configured → PASS (실행은 여전히 차단)
    if (
        side == "buy"
        and estimated <= max_krw
        and limit_price > 0
        and live_transport_status == "not_configured"
    ):
        return {
            "status": "PASS",
            "reasons": ["초소액 BUY_ONLY 후보", "한도 내", "차단 종목 아님"],
            "checks": {
                "execution_blocked": True,
                "adapter_status": adapter_status,
                "live_transport_status": live_transport_status,
                "live_order_allowed": live_order_allowed,
                "note": (
                    "Hermes PASS = 사용자 최종승인 검증 통과. "
                    "실주문 가능 여부는 stock-bot gate 별도 판단."
                ),
            },
        }

    # 6. otherwise → HOLD
    return {
        "status": "HOLD",
        "reasons": ["조건 불충분 — 수동 검토 필요"],
        "checks": {"verdict": "HOLD_manual_review"},
    }
