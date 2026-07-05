"""core/earnings_alert.py

보유종목 실적 발표 D-1/D-day 텔레그램 알림 — 지정가 예약/헤지 판단 타이밍용.

[동작]
1. settings HOLDINGS_* 전 계좌 티커 수집 (ETF는 fundamentals가 자동 skip)
2. fundamentals.fetch_financial_data로 실적일 + 확정/추정 + EPS 컨센서스 조회
3. days_to_earnings 0~1이면 알림 (ticker:earnings_date dedup — 종목당 1회)
4. KST 08~20시에만 동작 + 12시간 스로틀 → 사실상 하루 1회 아침 실행

[안전]
- 알림 전용, 주문 경로 없음. 조회 실패 시 조용히 스킵.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_THROTTLE_HOURS = 12
_ALERT_WINDOW_DAYS = 1  # D-1까지 알림
_MAX_SEEN = 200


def _state_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "earnings_alert_state.json"
    except Exception:
        return Path("db/data/earnings_alert_state.json")


def _load_state() -> dict:
    try:
        p = _state_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("earnings state load failed: %s", e)
    return {}


def _save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning("earnings state save failed: %s", e)


def _holding_tickers() -> dict[str, str]:
    """전 계좌 보유 티커 → 종목명 (PORTFOLIO에 없으면 티커 그대로)."""
    tickers: dict[str, str] = {}
    try:
        from config import settings
        names = getattr(settings, "PORTFOLIO", {}) or {}
        for attr in dir(settings):
            if not attr.startswith("HOLDINGS_") or attr == "HOLDINGS_AS_OF":
                continue
            value = getattr(settings, attr)
            if isinstance(value, dict):
                for tk in value:
                    tickers[tk] = names.get(tk, tk)
    except Exception as e:
        log.warning("earnings holdings scan failed: %s", e)
    return tickers


def _format_message(items: list[dict]) -> str:
    lines = ["📊 [실적 발표 임박] 보유종목 실적 알림"]
    for it in items:
        d_label = "오늘" if it["days_to"] == 0 else f"D-{it['days_to']}"
        status = "확정" if it["confirmed"] else (it["note"] or "추정")
        lines.append("")
        lines.append(f"🔔 {it['name']} ({it['ticker']}) — {it['date']} ({d_label}, {status})")
        detail = []
        if it["eps_estimate"]:
            detail.append(f"EPS 컨센서스 {it['eps_estimate']:,.2f}")
        if it["surprise_avg"]:
            detail.append(f"최근4Q 서프라이즈 평균 {it['surprise_avg']:+.1f}%")
        if detail:
            lines.append(f"  {' | '.join(detail)}")
    lines.append("")
    lines.append("발표 전후 변동성 주의 — 지정가 예약/부분 익절/헤지 검토")
    return "\n".join(lines)


def run_earnings_alert(now: datetime | None = None, force: bool = False) -> dict:
    """실적 D-1 알림 1회 실행. monitor 루프에서 주기 호출."""
    now = now or datetime.now(KST)
    state = _load_state()

    if not force:
        if not (8 <= now.hour < 20):  # KST 주간에만 (심야 알림 방지)
            return {"skipped": "off_hours"}
        last = state.get("last_checked_at", "")
        if last:
            try:
                if (now - datetime.fromisoformat(last)) < timedelta(hours=_THROTTLE_HOURS):
                    return {"skipped": "throttled"}
            except Exception:
                pass

    tickers = _holding_tickers()
    if not tickers:
        return {"skipped": "no_holdings"}

    state["last_checked_at"] = now.isoformat()

    from core.fundamentals import fetch_financial_data

    seen: list[str] = list(state.get("seen") or [])
    items: list[dict] = []
    checked = 0
    for tk, name in sorted(tickers.items()):
        fd = fetch_financial_data(tk, name)
        checked += 1
        if not fd or not fd.earnings_date:
            continue
        if not (0 <= fd.days_to_earnings <= _ALERT_WINDOW_DAYS):
            continue
        key = f"{tk}:{fd.earnings_date}"
        if key in seen:
            continue
        items.append({
            "ticker": tk, "name": name, "date": fd.earnings_date,
            "days_to": fd.days_to_earnings, "confirmed": fd.earnings_confirmed,
            "note": fd.earnings_note, "eps_estimate": fd.eps_estimate,
            "surprise_avg": fd.surprise_avg_4q, "key": key,
        })

    sent = False
    if items:
        try:
            from core.telegram import send_simple_message
            sent = send_simple_message(_format_message(items))
        except Exception as e:
            log.warning("earnings alert send failed: %s", e)
        if sent:
            seen.extend(it["key"] for it in items)
            state["seen"] = seen[-_MAX_SEEN:]

    _save_state(state)
    return {"checked": checked, "upcoming": len(items), "sent": sent}
