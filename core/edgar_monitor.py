"""core/edgar_monitor.py

SEC EDGAR 공시 모니터 — 미국 보유종목(NVDA/MU/LMT 등) 공시 감시. DART의 미국판.

[동작]
1. company_tickers.json으로 티커→CIK 매핑 (30일 파일 캐시)
2. data.sec.gov submissions API로 종목별 최근 공시 조회 (키 불필요)
3. 감시 대상 form(8-K/10-Q/10-K/S-3 등) 신규 건만 텔레그램 알림 (accession dedup)
4. 조회 실패 시 조용히 스킵 (fail-safe)

[안전]
- read-only GET만 사용, 주문 경로 변경 없음
- 알림 전용 — 자동 매도 발동 없음
- SEC 요구사항: 식별 가능한 User-Agent 필수
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_FILING_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    "&CIK={cik}&type={form}&dateb=&owner=include&count=10"
)

_UA = {"User-Agent": "SanjukStockMonitor personal-portfolio-alert admin@sanjuk.dev"}

# 감시 form → 심각도 (Form 4 인사이더 매매는 노이즈라 제외)
WATCH_FORMS: dict[str, str] = {
    "8-K": "high",       # 주요 이벤트 (실적/계약/경영진 변경)
    "10-Q": "medium",    # 분기 보고서
    "10-K": "medium",    # 연간 보고서
    "S-3": "high",       # 증권 발행 등록 (희석 리스크)
    "424B5": "high",     # 증자 프로스펙터스
    "SC 13D": "medium",  # 5%+ 지분 공시 (행동주의)
}

# 8-K Item 번호 → 한글 라벨 (submissions API items 필드 — 문서 파싱 불필요)
ITEM_LABELS: dict[str, str] = {
    "1.01": "중요 계약 체결",
    "1.02": "중요 계약 종료",
    "1.03": "파산/법정관리",
    "2.01": "자산 인수/처분 완료",
    "2.02": "실적 발표",
    "2.03": "채무/부외의무 발생",
    "2.05": "구조조정 비용",
    "2.06": "자산 손상",
    "3.01": "상장 유지 미달 통보",
    "4.01": "회계법인 변경",
    "4.02": "재무제표 신뢰 불가 (재작성)",
    "5.01": "지배권 변경",
    "5.02": "임원/이사 선임·사임",
    "5.03": "정관 변경",
    "5.07": "주주총회 결과",
    "7.01": "Reg FD 공정공시",
    "8.01": "기타 주요 이벤트",
    "9.01": "재무제표/증빙 첨부",
}

# 노이즈성 Item만으로 구성된 8-K는 심각도 하향 (예: 주총 결과, 증빙 첨부)
_LOW_SIGNAL_ITEMS = {"5.07", "7.01", "9.01"}

_CHECK_INTERVAL_MIN = 60   # 최소 재조회 간격 (미국 공시는 하루 수건 수준)
_MAX_SEEN = 300
_CIK_CACHE_TTL_SEC = 30 * 86400
_LOOKBACK_DAYS = 3


def _state_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "edgar_monitor_state.json"
    except Exception:
        return Path("db/data/edgar_monitor_state.json")


def _cik_cache_path() -> Path:
    return _state_path().parent / "edgar_cik_cache.json"


def _load_json(p: Path) -> dict:
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("edgar json load failed (%s): %s", p.name, e)
    return {}


def _save_json(p: Path, data: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning("edgar json save failed (%s): %s", p.name, e)


# ── CIK 매핑 ─────────────────────────────────────────────────────

def _cik_map() -> dict[str, int]:
    """티커 → CIK 매핑 (30일 파일 캐시)."""
    cache = _load_json(_cik_cache_path())
    if cache and time.time() - cache.get("saved_at", 0) < _CIK_CACHE_TTL_SEC:
        return {k: int(v) for k, v in (cache.get("map") or {}).items()}

    try:
        r = requests.get(_CIK_MAP_URL, headers=_UA, timeout=20)
        if r.status_code != 200:
            log.warning("EDGAR CIK map HTTP %s", r.status_code)
            return {k: int(v) for k, v in (cache.get("map") or {}).items()}
        mapping = {
            str(v.get("ticker", "")).upper(): int(v.get("cik_str", 0))
            for v in r.json().values()
            if v.get("ticker") and v.get("cik_str")
        }
        _save_json(_cik_cache_path(), {"saved_at": time.time(), "map": mapping})
        return mapping
    except Exception as e:
        log.warning("EDGAR CIK map fetch failed: %s", e)
        return {k: int(v) for k, v in (cache.get("map") or {}).items()}


def _us_holding_tickers() -> list[str]:
    """settings HOLDINGS_* 에서 미국 티커(점 없는 심볼) 추출."""
    tickers: set[str] = set()
    try:
        from config import settings
        for name in dir(settings):
            if not name.startswith("HOLDINGS_"):
                continue
            value = getattr(settings, name)
            if isinstance(value, dict):
                tickers.update(tk for tk in value if "." not in tk)
    except Exception as e:
        log.warning("edgar holdings scan failed: %s", e)
    return sorted(tickers)


# ── 공시 조회 ────────────────────────────────────────────────────

def fetch_recent_filings(ticker: str, cik: int, days: int = _LOOKBACK_DAYS) -> list[dict]:
    """단일 종목 최근 공시 중 감시 form만 반환. 실패 시 빈 리스트."""
    try:
        r = requests.get(_SUBMISSIONS_URL.format(cik=cik), headers=_UA, timeout=15)
        if r.status_code != 200:
            log.warning("EDGAR submissions HTTP %s (%s)", r.status_code, ticker)
            return []
        recent = (r.json().get("filings") or {}).get("recent") or {}
    except Exception as e:
        log.warning("EDGAR submissions fetch failed (%s): %s", ticker, e)
        return []

    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocDescription") or []
    items_list = recent.get("items") or []

    cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    hits: list[dict] = []
    for i, form in enumerate(forms):
        form = str(form).strip()
        if form not in WATCH_FORMS:
            continue
        filing_date = str(dates[i]) if i < len(dates) else ""
        if filing_date < cutoff:
            break  # recent은 최신순 — 컷오프 이전이면 중단

        severity = WATCH_FORMS[form]
        raw_items = str(items_list[i]) if i < len(items_list) else ""
        item_nums = [s.strip() for s in raw_items.split(",") if s.strip()]
        item_labels = [
            f"{n} {ITEM_LABELS[n]}" if n in ITEM_LABELS else n for n in item_nums
        ]
        # 8-K가 노이즈성 Item(주총 결과/공정공시/증빙)뿐이면 medium으로 하향
        if form == "8-K" and item_nums and all(n in _LOW_SIGNAL_ITEMS for n in item_nums):
            severity = "medium"

        hits.append({
            "ticker": ticker,
            "form": form,
            "severity": severity,
            "filing_date": filing_date,
            "accession": str(accessions[i]) if i < len(accessions) else "",
            "description": str(docs[i]) if i < len(docs) else "",
            "items": item_labels,
            "url": _FILING_URL.format(cik=cik, form=form),
        })
    return hits


# ── 메시지 ───────────────────────────────────────────────────────

_SEVERITY_ICON = {"high": "🚨", "medium": "⚠️"}


def _format_alert_message(hits: list[dict]) -> str:
    lines = ["📢 [SEC EDGAR 알림] 미국 보유종목 신규 공시"]
    for h in hits:
        icon = _SEVERITY_ICON.get(h.get("severity", ""), "⚠️")
        lines.append("")
        lines.append(f"{icon} {h.get('ticker')} — {h.get('form')}")
        for label in h.get("items") or []:
            lines.append(f"  · {label}")
        desc = h.get("description") or ""
        if desc and desc != h.get("form"):
            lines.append(f"  {desc[:80]}")
        lines.append(f"  접수일: {h.get('filing_date')}")
        lines.append(f"  {h.get('url')}")
    lines.append("")
    lines.append("자동 매도는 발동하지 않음 — 내용 확인 후 직접 판단 필요")
    return "\n".join(lines)


# ── 메인 루틴 ────────────────────────────────────────────────────

def run_edgar_monitor(now: datetime | None = None, force: bool = False) -> dict:
    """EDGAR 공시 모니터 1회 실행. monitor 루프에서 주기 호출."""
    now = now or datetime.now(KST)

    state = _load_json(_state_path())

    if not force:
        if now.weekday() == 6:  # 일요일(KST)만 스킵 — 토요일 새벽=미 금요일 공시
            return {"skipped": "sunday"}
        last = state.get("last_checked_at", "")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (now - last_dt) < timedelta(minutes=_CHECK_INTERVAL_MIN):
                    return {"skipped": "throttled"}
            except Exception:
                pass

    tickers = _us_holding_tickers()
    if not tickers:
        return {"skipped": "no_us_holdings"}

    cik_map = _cik_map()
    state["last_checked_at"] = now.isoformat()

    hits: list[dict] = []
    for tk in tickers:
        cik = cik_map.get(tk.upper())
        if not cik:
            continue
        hits.extend(fetch_recent_filings(tk, cik))
        time.sleep(0.15)  # SEC rate limit 예의 (10 req/s 한도)

    seen: list[str] = list(state.get("seen_accessions") or [])
    new_hits = [h for h in hits if h.get("accession") and h["accession"] not in seen]

    sent = False
    if new_hits:
        try:
            from core.telegram import send_simple_message
            sent = send_simple_message(_format_alert_message(new_hits))
        except Exception as e:
            log.warning("edgar alert send failed: %s", e)
        if sent:
            seen.extend(h["accession"] for h in new_hits)
            state["seen_accessions"] = seen[-_MAX_SEEN:]

    _save_json(_state_path(), state)
    return {
        "checked": True,
        "tickers": tickers,
        "hit_count": len(hits),
        "new_hit_count": len(new_hits),
        "sent": sent,
    }
