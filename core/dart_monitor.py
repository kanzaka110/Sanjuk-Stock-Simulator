"""core/dart_monitor.py

DART 전자공시 모니터 — Toss 보유종목 리스크 공시 감시.

[동작]
1. DART OpenAPI list.json으로 최근 공시 목록 조회 (env DART_API_KEY 필요)
2. Toss 보유종목 + exit watch 심볼의 리스크 키워드 공시만 필터
3. 신규 공시(rcept_no dedup)만 텔레그램 알림
4. 키 미설정/조회 실패 시 조용히 스킵 (fail-safe — 자동매매 차단 없음)

[안전]
- read-only GET만 사용, 주문 경로 변경 없음
- 알림 전용 — 공시 발견이 자동 매도를 직접 발동하지 않음
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_DART_VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

# 리스크 키워드 → 심각도
RISK_KEYWORDS: dict[str, str] = {
    "유상증자": "high",
    "무상감자": "high",
    "감자결정": "high",
    "관리종목": "high",
    "거래정지": "high",
    "상장폐지": "high",
    "불성실공시": "high",
    "횡령": "high",
    "배임": "high",
    "회생절차": "high",
    "파산": "high",
    "감사의견": "high",
    "전환사채": "medium",
    "신주인수권부사채": "medium",
    "교환사채": "medium",
    "최대주주변경": "medium",
    "소송": "medium",
    "공개매수": "medium",
    "영업정지": "high",
    "자본잠식": "high",
}

_CHECK_INTERVAL_MIN = 30  # 최소 재조회 간격
_CHECK_HOUR_START = 8     # KST 08시 ~
_CHECK_HOUR_END = 18      # KST 18시
_MAX_SEEN = 500           # state에 유지할 rcept_no 수


def _api_key() -> str:
    return (os.environ.get("DART_API_KEY") or "").strip()


def _state_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "dart_monitor_state.json"
    except Exception:
        return Path("db/data/dart_monitor_state.json")


def _load_state() -> dict:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("dart state load failed: %s", e)
    return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        log.warning("dart state save failed: %s", e)


# ── 공시 조회 ────────────────────────────────────────────────────

def fetch_recent_disclosures(days: int = 2, now: datetime | None = None) -> dict:
    """DART 최근 공시 목록 조회. 실패 시 {"ok": False, "reason": ...}."""
    key = _api_key()
    if not key:
        return {"ok": False, "reason": "no_api_key", "items": []}

    now = now or datetime.now(KST)
    bgn = (now - timedelta(days=days)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")

    try:
        import requests
        r = requests.get(
            _DART_LIST_URL,
            params={
                "crtfc_key": key,
                "bgn_de": bgn,
                "end_de": end,
                "page_count": 100,
            },
            timeout=15,
        )
        if r.status_code != 200:
            return {"ok": False, "reason": f"http_{r.status_code}", "items": []}
        data = r.json()
    except Exception as e:
        log.warning("DART list fetch failed: %s", e)
        return {"ok": False, "reason": f"fetch_error: {e}", "items": []}

    status = str(data.get("status", ""))
    if status == "013":  # 조회 데이터 없음
        return {"ok": True, "items": []}
    if status != "000":
        return {"ok": False, "reason": f"dart_status_{status}", "items": []}

    items = []
    for row in data.get("list") or []:
        items.append({
            "rcept_no": str(row.get("rcept_no") or ""),
            "rcept_dt": str(row.get("rcept_dt") or ""),
            "stock_code": str(row.get("stock_code") or "").strip(),
            "corp_name": str(row.get("corp_name") or ""),
            "report_nm": str(row.get("report_nm") or ""),
        })
    return {"ok": True, "items": items}


def screen_disclosures(items: list[dict], stock_codes: set[str]) -> list[dict]:
    """보유종목 공시 중 리스크 키워드 포함 건만 반환."""
    hits: list[dict] = []
    for it in items:
        code = it.get("stock_code") or ""
        if not code or code not in stock_codes:
            continue
        report_nm = it.get("report_nm") or ""
        for keyword, severity in RISK_KEYWORDS.items():
            if keyword in report_nm:
                hits.append({
                    **it,
                    "keyword": keyword,
                    "severity": severity,
                    "url": _DART_VIEW_URL.format(rcept_no=it.get("rcept_no", "")),
                })
                break
    return hits


def _toss_holding_codes() -> set[str]:
    """Toss 보유종목 6자리 코드 집합 (조회 실패 시 빈 set)."""
    codes: set[str] = set()
    try:
        from core.dashboard_data import _toss_holding_price_map
        for sym in _toss_holding_price_map():
            base = sym.split(".")[0]
            if base.isdigit() and len(base) == 6:
                codes.add(base)
    except Exception as e:
        log.debug("toss holding codes fetch failed: %s", e)
    return codes


# ── 메시지 ───────────────────────────────────────────────────────

_SEVERITY_ICON = {"high": "🚨", "medium": "⚠️"}


def _format_alert_message(hits: list[dict]) -> str:
    lines = ["📢 [DART 공시 알림] 보유종목 리스크 공시"]
    for h in hits:
        icon = _SEVERITY_ICON.get(h.get("severity", ""), "⚠️")
        lines.append("")
        lines.append(f"{icon} {h.get('corp_name')} ({h.get('stock_code')})")
        lines.append(f"  {h.get('report_nm')}")
        lines.append(f"  키워드: {h.get('keyword')} · 접수일: {h.get('rcept_dt')}")
        lines.append(f"  {h.get('url')}")
    lines.append("")
    lines.append("자동 매도는 발동하지 않음 — 내용 확인 후 직접 판단 필요")
    return "\n".join(lines)


# ── 메인 루틴 ────────────────────────────────────────────────────

def run_dart_monitor(now: datetime | None = None, force: bool = False) -> dict:
    """DART 공시 모니터 1회 실행. monitor 루프에서 주기 호출."""
    now = now or datetime.now(KST)

    if not _api_key():
        return {"skipped": "no_api_key"}

    if not force:
        if now.weekday() >= 5:
            return {"skipped": "weekend"}
        if not (_CHECK_HOUR_START <= now.hour < _CHECK_HOUR_END):
            return {"skipped": "outside_hours"}

    state = _load_state()

    if not force:
        last = state.get("last_checked_at", "")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (now - last_dt) < timedelta(minutes=_CHECK_INTERVAL_MIN):
                    return {"skipped": "throttled"}
            except Exception:
                pass

    codes = _toss_holding_codes()
    if not codes:
        state["last_checked_at"] = now.isoformat()
        _save_state(state)
        return {"skipped": "no_holdings"}

    fetched = fetch_recent_disclosures(now=now)
    state["last_checked_at"] = now.isoformat()
    if not fetched.get("ok"):
        _save_state(state)
        return {"skipped": fetched.get("reason", "fetch_failed")}

    hits = screen_disclosures(fetched["items"], codes)

    seen: list[str] = list(state.get("seen_rcept_nos") or [])
    new_hits = [h for h in hits if h.get("rcept_no") and h["rcept_no"] not in seen]

    sent = False
    if new_hits:
        try:
            from core.telegram import send_simple_message
            sent = send_simple_message(_format_alert_message(new_hits))
        except Exception as e:
            log.warning("dart alert send failed: %s", e)
        if sent:
            seen.extend(h["rcept_no"] for h in new_hits)
            state["seen_rcept_nos"] = seen[-_MAX_SEEN:]

    _save_state(state)
    return {
        "checked": True,
        "holdings_count": len(codes),
        "hit_count": len(hits),
        "new_hit_count": len(new_hits),
        "sent": sent,
    }
