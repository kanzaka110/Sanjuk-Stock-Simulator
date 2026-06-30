"""
Toss 판단 컨텍스트 어댑터 — read-only

브리핑/시뮬레이터가 쓰기 쉬운 형태로 Toss 계좌 정보를 정규화한다.
실패해도 브리핑 전체 실패시키지 않고 warning으로 degrade.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# ─── 캐시 (60초) ────────────────────────────────────
_cache_lock = threading.Lock()
_cache_data: dict | None = None
_cache_ts: float = 0.0
_CACHE_TTL = 60


def get_toss_decision_context() -> dict:
    """Toss read-only 정보를 판단 컨텍스트로 반환. 실패 시 빈 컨텍스트."""
    global _cache_data, _cache_ts

    now = time.monotonic()
    with _cache_lock:
        if _cache_data and now - _cache_ts < _CACHE_TTL:
            return _cache_data

    ctx = _fetch_context()
    with _cache_lock:
        _cache_data = ctx
        _cache_ts = now
    return ctx


def _fetch_context() -> dict:
    """Toss API 호출 후 정규화된 컨텍스트 반환."""
    base = {
        "enabled": False,
        "account_label": "Toss 실전 AI 자동거래 계좌",
        "included_in_total_portfolio": False,
        "cash_krw": 0,
        "cash_usd": None,
        "market_value_krw": 0,
        "total_account_value_krw": 0,
        "holdings_count": 0,
        "holdings": [],
        "usdkrw": 0,
        "market_calendar": {"KR": {}, "US": {}},
        "automation": {
            "enabled": False,
            "mode": "paper",
            "dry_run": True,
            "live_orders_allowed": False,
            "kill_switch": True,
        },
        "data_quality": {
            "toss_available": False,
            "cash_available": False,
            "fx_available": False,
            "calendar_available": False,
            "stale": False,
            "warnings": [],
        },
    }

    try:
        from core import toss_client as tc
        if not tc.is_configured():
            base["data_quality"]["warnings"].append("Toss API 미설정")
            return base

        base["enabled"] = True
        base["data_quality"]["toss_available"] = True
        warnings = base["data_quality"]["warnings"]

        # 계좌 목록
        accounts = tc.get_accounts()
        if not accounts:
            warnings.append("계좌 목록 조회 실패")
            return base

        seq = str(accounts[0].get("accountSeq", ""))

        # 보유종목
        holdings = tc.get_holdings(seq)
        items = holdings.get("items", [])
        base["holdings_count"] = len(items)
        base["holdings"] = tc.sanitize_dict(items)

        mv = holdings.get("marketValue", {})
        mv_amt = mv.get("amount", {}) if isinstance(mv, dict) else {}
        try:
            base["market_value_krw"] = float(mv_amt.get("krw", "0") or "0")
        except (ValueError, TypeError):
            pass

        # 현금
        bp_krw = tc.get_buying_power(seq, "KRW")
        if bp_krw:
            try:
                base["cash_krw"] = float(bp_krw.get("cashBuyingPower", "0"))
                base["data_quality"]["cash_available"] = True
            except (ValueError, TypeError):
                warnings.append("KRW 예수금 파싱 실패")
        else:
            warnings.append("KRW 예수금 조회 실패")

        bp_usd = tc.get_buying_power(seq, "USD")
        if bp_usd:
            try:
                v = float(bp_usd.get("cashBuyingPower", "0"))
                base["cash_usd"] = v if v > 0 else None
            except (ValueError, TypeError):
                pass

        base["total_account_value_krw"] = base["cash_krw"] + base["market_value_krw"]

        # 환율
        fx = tc.get_exchange_rate("USD", "KRW")
        if fx:
            try:
                base["usdkrw"] = float(fx.get("rate", 0))
                base["data_quality"]["fx_available"] = True
            except (ValueError, TypeError):
                warnings.append("환율 파싱 실패")
        else:
            warnings.append("환율 조회 실패")

        # 장 캘린더
        for mkt in ("KR", "US"):
            cal = tc.get_market_calendar(mkt)
            if cal:
                base["market_calendar"][mkt] = cal
                base["data_quality"]["calendar_available"] = True
            else:
                warnings.append(f"{mkt} 캘린더 조회 실패")

        # 자동화 상태
        try:
            from config import toss_automation as cfg
            base["automation"] = {
                "enabled": cfg.TOSS_AUTOMATION_ENABLED,
                "mode": cfg.TOSS_AUTOMATION_MODE,
                "dry_run": cfg.TOSS_DRY_RUN,
                "live_orders_allowed": cfg.TOSS_ALLOW_LIVE_ORDERS,
                "kill_switch": cfg.TOSS_KILL_SWITCH,
            }
        except Exception:
            pass

    except Exception as e:
        logger.warning("Toss context fetch failed: %s", str(e)[:100])
        base["data_quality"]["warnings"].append(f"조회 실패: {str(e)[:50]}")

    return base


def context_to_briefing_text(ctx: dict | None = None) -> str:
    """브리핑 LLM 입력용 텍스트 블록 생성."""
    if ctx is None:
        ctx = get_toss_decision_context()
    if not ctx.get("enabled"):
        return ""

    auto = ctx.get("automation", {})
    dq = ctx.get("data_quality", {})
    warns = dq.get("warnings", [])

    lines = [
        "[Toss 실전 AI 자동거래 계좌 — read-only]",
        "- 기존 포트폴리오 미합산",
        f"- 현금: ₩{ctx['cash_krw']:,.0f}",
    ]
    if ctx.get("cash_usd"):
        lines.append(f"- USD 현금: ${ctx['cash_usd']:,.2f}")
    lines.extend([
        f"- 보유종목 평가: ₩{ctx['market_value_krw']:,.0f} ({ctx['holdings_count']}종목)",
        f"- 총 Toss 자산: ₩{ctx['total_account_value_krw']:,.0f}",
        f"- 자동거래: {'활성' if auto.get('enabled') else '비활성'} / {auto.get('mode', 'paper')} / dry_run={auto.get('dry_run', True)}",
        f"- 실주문 허용: {auto.get('live_orders_allowed', False)}",
        f"- 킬스위치: {'ON' if auto.get('kill_switch', True) else 'OFF'}",
    ])
    if ctx.get("usdkrw"):
        lines.append(f"- USD/KRW: {ctx['usdkrw']:,.1f}")
    lines.append("- 사용 목적: 후보 검증 및 paper 판단 보조, 실제 주문 아님")
    if warns:
        lines.append(f"- 경고: {', '.join(warns)}")
    return "\n".join(lines)



def format_toss_live_pilot_briefing_lessons(limit: int = 8) -> str:
    """Toss live pilot 운영 교훈을 다음 브리핑 LLM 입력으로 제공한다.

    목적: 오늘의 자동화 테스트/실패 원인을 다음 브리핑이 잊지 않도록
    read-only 이벤트/정책 요약과 실행 규칙을 주입한다.
    """
    try:
        from core.toss_live_pilot_events import list_events
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        from core.toss_live_transport import get_transport_status
    except Exception:
        return ""

    try:
        policy = compute_toss_live_pilot_policy()
    except Exception:
        policy = {}
    try:
        transport = get_transport_status()
    except Exception:
        transport = {}
    try:
        events = list_events(limit)
    except Exception:
        events = []

    side_mode = policy.get("side_mode", "UNKNOWN")
    allowed_sides = ",".join(policy.get("allowed_sides") or []) or "unknown"
    sell_allowed = bool(policy.get("sell_allowed"))
    live_possible = bool(
        (policy.get("transport") or {}).get("live_order_sent_possible")
        or transport.get("live_order_sent_possible")
        or (policy.get("live_transport_status") == "configured" and policy.get("live_order_allowed"))
    )

    lines = [
        "[Toss Live Pilot — 브리핑 반영 규칙]",
        f"- 현재 live pilot: side_mode={side_mode}, allowed_sides={allowed_sides}, sell_allowed={sell_allowed}, live_order_sent_possible={live_possible}",
        "- 토스 AI 계좌는 미국장 BUY+SELL 소액 자동화 우선. 삼성증권 자동화로 해석 금지.",
        "- BUY 성공 직후 SELL은 Toss 보유/매도가능수량 반영 지연으로 422가 날 수 있음.",
        "- 다음 브리핑/자동화 판단: 매수 직후 매도는 holdings에서 매도가능수량 확인 후 실행. 실패 시 사용자에게 수동 매도 여부를 묻지 말고 원인확인→재시도/패치→검증으로 닫기.",
        "- 주문표에는 체결/금액/수량/잔고/손익, 즉시체결 위험, 손절/무효화를 우선 표시.",
    ]

    recent = []
    for e in events[:5]:
        symbol = e.get("symbol") or ""
        if not symbol:
            continue
        side = e.get("side") or ""
        et = e.get("event_type") or ""
        sent = bool(e.get("live_order_sent"))
        reason = e.get("reason") or ""
        recent.append(f"  · {symbol} {side}: {et}, live_order_sent={sent}, reason={reason}")
    if recent:
        lines.append("- 최근 live pilot 이벤트:")
        lines.extend(recent)
    return "\n".join(lines)
