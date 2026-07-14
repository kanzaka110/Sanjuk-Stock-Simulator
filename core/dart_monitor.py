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
import re
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from core.source_observation_collectors import (
    CollectorResult,
    record_dart_disclosure_observations,
)
from core.source_observations import SourceObservationStore

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


KST = timezone(timedelta(hours=9))

_DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_DART_VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
_DART_PAGE_WORKERS = 5
_MAX_DART_PAGES = 20
_DART_HTTP_TIMEOUT = (3.0, 5.0)
_DART_TOTAL_TIMEOUT_SECONDS = 45.0
_SAFE_ERROR_TYPE_RE = re.compile(
    r"^(?:no_api_key|fetch_failed|fetch_error:[A-Z][A-Za-z0-9_]{0,48}(?:Error|Exception|Timeout)|"
    r"http_[1-5][0-9]{2}|dart_status_[A-Za-z0-9_-]{1,32}|"
    r"collector_timeout|pagination_invalid|pagination_limit|partial_fetch)$"
)

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
    """DART 최근 공시 전체 페이지 조회. 한 페이지라도 실패하면 전부 폐기한다."""
    key = _api_key()
    if not key:
        return {"ok": False, "reason": "no_api_key", "items": []}

    now = now or datetime.now(KST)
    bgn = (now - timedelta(days=days)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")

    import requests

    def fetch_page(page_no: int) -> dict:
        try:
            response = requests.get(
                _DART_LIST_URL,
                params={
                    "crtfc_key": key,
                    "bgn_de": bgn,
                    "end_de": end,
                    "page_count": 100,
                    "page_no": page_no,
                },
                timeout=_DART_HTTP_TIMEOUT,
            )
            if response.status_code != 200:
                return {
                    "ok": False,
                    "reason": f"http_{response.status_code}",
                    "items": [],
                }
            data = response.json()
        except Exception as exc:
            error_type = type(exc).__name__
            log.warning("DART list fetch failed: %s", error_type)
            return {
                "ok": False,
                "reason": f"fetch_error:{error_type}",
                "items": [],
            }

        status = str(data.get("status", ""))
        if status == "013" and page_no == 1:
            return {
                "ok": True,
                "page_no": page_no,
                "total_page": 1,
                "total_count": 0,
                "items": [],
            }
        if status != "000":
            return {
                "ok": False,
                "reason": f"dart_status_{status}",
                "items": [],
            }
        try:
            total_page = int(data.get("total_page") or 1)
            raw_total_count = data.get("total_count")
            if raw_total_count in (None, ""):
                if total_page != 1:
                    raise ValueError("missing_total_count")
                total_count = len(data.get("list") or [])
            else:
                total_count = int(raw_total_count)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "pagination_invalid", "items": []}
        if not (1 <= total_page <= _MAX_DART_PAGES) or total_count < 0:
            reason = "pagination_limit" if total_page > _MAX_DART_PAGES else "pagination_invalid"
            return {"ok": False, "reason": reason, "items": []}

        page_items = [
            {
                "rcept_no": str(row.get("rcept_no") or ""),
                "rcept_dt": str(row.get("rcept_dt") or ""),
                "stock_code": str(row.get("stock_code") or "").strip(),
                "corp_name": str(row.get("corp_name") or ""),
                "report_nm": str(row.get("report_nm") or ""),
            }
            for row in data.get("list") or []
        ]
        return {
            "ok": True,
            "page_no": page_no,
            "total_page": total_page,
            "total_count": total_count,
            "items": page_items,
        }

    deadline = time.monotonic() + _DART_TOTAL_TIMEOUT_SECONDS
    executor = ThreadPoolExecutor(max_workers=_DART_PAGE_WORKERS)
    first_future = executor.submit(fetch_page, 1)
    try:
        first = first_future.result(timeout=max(0.0, deadline - time.monotonic()))
    except FuturesTimeoutError:
        first_future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return {"ok": False, "reason": "collector_timeout", "items": []}
    except Exception as exc:
        error_type = type(exc).__name__
        executor.shutdown(wait=False, cancel_futures=True)
        return {
            "ok": False,
            "reason": f"fetch_error:{error_type}",
            "items": [],
        }
    if not first["ok"]:
        executor.shutdown(wait=True)
        return first
    total_page = int(first["total_page"])
    total_count = int(first["total_count"])
    pages = {1: first}
    if total_page > 1:
        futures = {
            executor.submit(fetch_page, page_no): page_no
            for page_no in range(2, total_page + 1)
        }
        failure: dict | None = None
        try:
            remaining = max(0.0, deadline - time.monotonic())
            for future in as_completed(futures, timeout=remaining):
                page_no = futures[future]
                try:
                    page = future.result()
                except Exception as exc:
                    error_type = type(exc).__name__
                    log.warning("DART page worker failed: %s", error_type)
                    failure = {
                        "ok": False,
                        "reason": f"fetch_error:{error_type}",
                        "items": [],
                    }
                    break
                if not page["ok"]:
                    failure = page
                    break
                if (
                    int(page["total_page"]) != total_page
                    or int(page["total_count"]) != total_count
                ):
                    failure = {
                        "ok": False,
                        "reason": "pagination_invalid",
                        "items": [],
                    }
                    break
                pages[page_no] = page
        except FuturesTimeoutError:
            failure = {"ok": False, "reason": "collector_timeout", "items": []}
        if failure is not None:
            for pending in futures:
                pending.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            return failure
    executor.shutdown(wait=True)

    items = [
        item
        for page_no in range(1, total_page + 1)
        for item in pages[page_no]["items"]
    ]
    if total_count != len(items):
        return {"ok": False, "reason": "partial_fetch", "items": []}
    return {
        "ok": True,
        "items": items,
        "pages_fetched": total_page,
        "total_count": total_count,
    }


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

def _record_failed_observation_run(
    *,
    started_at: datetime,
    reason: str,
    observation_store: SourceObservationStore | None,
) -> tuple[str, str]:
    candidate = str(reason).strip()
    safe_reason = candidate if _SAFE_ERROR_TYPE_RE.fullmatch(candidate) else "fetch_failed"
    try:
        store = observation_store or SourceObservationStore(
            _state_path().parent / "source_observations.db"
        )
        collected_at = _utc_now()
        run = store.record_collection_run(
            source="opendart_disclosures",
            run_id=(
                f"dart:{collected_at.strftime('%Y%m%dT%H%M%S%fZ')}:{uuid4().hex}"
            ),
            started_at=started_at,
            completed_at=collected_at,
            rows_seen=0,
            rows_inserted=0,
            rows_duplicate=0,
            rows_skipped=0,
            rows_invalid=0,
            error_type=safe_reason or "fetch_failed",
        )
        return run.status, ""
    except Exception as e:
        log.warning("DART failed-run persistence failed: %s", e)
        return "", type(e).__name__


def run_dart_monitor(
    now: datetime | None = None,
    force: bool = False,
    observation_store: SourceObservationStore | None = None,
) -> dict:
    """DART 공시 모니터 1회 실행. monitor 루프에서 주기 호출."""
    run_started_at = _utc_now()
    now = now or datetime.now(KST)

    if not _api_key():
        run_status, observation_error = _record_failed_observation_run(
            started_at=run_started_at,
            reason="no_api_key",
            observation_store=observation_store,
        )
        return {
            "skipped": "no_api_key",
            "observation_error": observation_error,
            "observation_run_status": run_status,
        }

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
    if not fetched.get("ok"):
        reason = str(fetched.get("reason") or "fetch_failed")
        observation_run_status, observation_error = _record_failed_observation_run(
            started_at=run_started_at,
            reason=reason,
            observation_store=observation_store,
        )
        _save_state(state)
        return {
            "skipped": reason,
            "observation_error": observation_error,
            "observation_run_status": observation_run_status,
        }

    state["last_checked_at"] = now.isoformat()
    items = fetched["items"]
    observation_result = CollectorResult(0, 0, 0, 0, 0)
    observation_error = ""
    observation_run_status = ""
    store = observation_store
    collected_at = _utc_now()
    run_id = f"dart:{collected_at.strftime('%Y%m%dT%H%M%S%fZ')}:{uuid4().hex}"
    try:
        store = store or SourceObservationStore(
            _state_path().parent / "source_observations.db"
        )
        with store.atomic_write():
            observation_result = record_dart_disclosure_observations(
                items,
                store=store,
                ingested_at=collected_at,
            )
            run = store.record_collection_run(
                source="opendart_disclosures",
                run_id=run_id,
                started_at=run_started_at,
                completed_at=collected_at,
                rows_seen=observation_result.seen,
                rows_inserted=observation_result.inserted,
                rows_duplicate=observation_result.duplicates,
                rows_skipped=observation_result.skipped,
                rows_invalid=observation_result.invalid,
                error_type="",
            )
        observation_run_status = run.status
    except Exception as exc:
        observation_error = type(exc).__name__
        observation_result = CollectorResult(len(items), 0, 0, 0, 0)
        log.warning("DART observation persistence failed: %s", observation_error)
        if store is not None:
            try:
                failed_run = store.record_collection_run(
                    source="opendart_disclosures",
                    run_id=run_id,
                    started_at=run_started_at,
                    completed_at=_utc_now(),
                    rows_seen=len(items),
                    rows_inserted=0,
                    rows_duplicate=0,
                    rows_skipped=0,
                    rows_invalid=0,
                    error_type=f"persistence_{observation_error}"[:128],
                )
                observation_run_status = failed_run.status
            except Exception as run_exc:
                log.warning(
                    "DART failed-run persistence failed: %s",
                    type(run_exc).__name__,
                )

    hits = screen_disclosures(items, codes)

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
        "observation_seen": observation_result.seen,
        "observation_inserted": observation_result.inserted,
        "observation_duplicates": observation_result.duplicates,
        "observation_skipped": observation_result.skipped,
        "observation_invalid": observation_result.invalid,
        "observation_error": observation_error,
        "observation_run_status": observation_run_status,
    }
