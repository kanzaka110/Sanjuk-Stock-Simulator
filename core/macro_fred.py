"""
FRED 매크로 지표 — 미 연준 세인트루이스 공개 데이터

API 키가 필요 없는 fredgraph.csv 공개 엔드포인트 사용 (시리즈당 1요청).
ECONOMIC_CALENDAR 수동 갱신과 별개로 금리/물가/고용 '현재 수준'을 라이브 공급.
6시간 파일 캐시 — 브리핑은 단명 프로세스라 프로세스 캐시로는 부족.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_CACHE_FILE = Path(__file__).resolve().parent.parent / "db" / "data" / "fred_cache.json"
_CACHE_TTL_SEC = 6 * 3600

# 시리즈: (FRED ID, 설명)
SERIES = {
    "T10Y2Y": "10Y-2Y 금리차",
    "DGS10": "미 10년물 금리",
    "DFF": "연방기금금리",
    "CPIAUCSL": "CPI (지수)",
    "UNRATE": "실업률",
}


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text())
            if time.time() - data.get("saved_at", 0) < _CACHE_TTL_SEC:
                return data.get("series", {})
    except Exception as e:
        log.warning("FRED 캐시 읽기 실패: %s", e)
    return {}


def _save_cache(series: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"saved_at": time.time(), "series": series})
        )
    except Exception as e:
        log.warning("FRED 캐시 저장 실패: %s", e)


def fetch_series(series_id: str) -> list[tuple[str, float]]:
    """FRED 단일 시리즈 조회 → [(YYYY-MM-DD, value), ...] 오래된순.

    실패 시 빈 리스트. '.'(결측) 행은 제외.
    """
    import subprocess
    from datetime import date, timedelta

    if not re.fullmatch(r"[A-Z0-9]+", series_id):
        log.warning("FRED 잘못된 시리즈 ID: %r", series_id)
        return []

    start = (date.today() - timedelta(days=730)).isoformat()  # 최근 2년만 (payload 절감)
    url = f"{FRED_CSV_URL}?id={series_id}&cosd={start}"
    try:
        # FRED는 python-requests TLS 핑거프린트를 차단 (Akamai) → curl 사용
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", "20", url],
            capture_output=True, text=True, timeout=25,
        )
        if proc.returncode != 0:
            log.warning("FRED %s curl 실패: %s", series_id, proc.stderr.strip()[:200])
            return []
        rows: list[tuple[str, float]] = []
        for line in proc.stdout.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) != 2 or parts[1] in (".", ""):
                continue
            try:
                rows.append((parts[0], float(parts[1])))
            except ValueError:
                continue
        return rows
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("FRED %s 조회 실패: %s", series_id, e)
        return []


def fetch_macro_snapshot() -> dict:
    """FRED 핵심 매크로 스냅샷. 캐시 우선, 실패 시리즈는 생략.

    Returns:
        {"t10y2y": {...}, "dgs10": {...}, "dff": {...},
         "cpi_yoy": {...}, "unrate": {...}, "as_of": "..."} — 전부 실패 시 {}.
    """
    cached = _load_cache()
    if cached:
        return cached

    raw: dict[str, list] = {}
    for sid in SERIES:
        rows = fetch_series(sid)
        if rows:
            raw[sid] = rows[-400:]  # 최근 ~1.3년치만 유지

    if not raw:
        return {}

    snap: dict = {}

    def latest(sid: str) -> tuple[str, float] | None:
        rows = raw.get(sid)
        return rows[-1] if rows else None

    # 10Y-2Y 금리차 (역전 여부)
    v = latest("T10Y2Y")
    if v:
        snap["t10y2y"] = {
            "value": v[1], "date": v[0],
            "label": "역전(침체 신호)" if v[1] < 0 else "정상",
        }

    # 10년물 금리 + 20영업일 변화
    rows = raw.get("DGS10", [])
    if rows:
        cur = rows[-1]
        chg = round(cur[1] - rows[-21][1], 2) if len(rows) >= 21 else 0.0
        snap["dgs10"] = {"value": cur[1], "date": cur[0], "chg_20d": chg}

    # 연방기금금리
    v = latest("DFF")
    if v:
        snap["dff"] = {"value": v[1], "date": v[0]}

    # CPI YoY (지수 → 전년동월비)
    rows = raw.get("CPIAUCSL", [])
    if len(rows) >= 13:
        cur, prev = rows[-1], rows[-13]
        yoy = round((cur[1] / prev[1] - 1) * 100, 2)
        snap["cpi_yoy"] = {"value": yoy, "date": cur[0]}

    # 실업률 + 3개월 변화
    rows = raw.get("UNRATE", [])
    if rows:
        cur = rows[-1]
        chg = round(cur[1] - rows[-4][1], 1) if len(rows) >= 4 else 0.0
        snap["unrate"] = {"value": cur[1], "date": cur[0], "chg_3m": chg}

    if snap:
        snap["as_of"] = time.strftime("%Y-%m-%d %H:%M")
        _save_cache(snap)
    return snap


def macro_to_text(snap: dict) -> str:
    """FRED 스냅샷 → 브리핑 컨텍스트 텍스트. 빈 스냅샷이면 빈 문자열."""
    if not snap:
        return ""
    lines = ["【미국 매크로 (FRED 공식)】"]
    t = snap.get("t10y2y")
    if t:
        lines.append(f"  10Y-2Y 금리차: {t['value']:+.2f}%p [{t['label']}] ({t['date']})")
    d = snap.get("dgs10")
    if d:
        lines.append(f"  미 10년물: {d['value']:.2f}% (20일 {d['chg_20d']:+.2f}%p)")
    f = snap.get("dff")
    if f:
        lines.append(f"  연방기금금리: {f['value']:.2f}%")
    c = snap.get("cpi_yoy")
    if c:
        lines.append(f"  CPI YoY: {c['value']:.1f}% ({c['date'][:7]})")
    u = snap.get("unrate")
    if u:
        lines.append(f"  실업률: {u['value']:.1f}% (3개월 {u['chg_3m']:+.1f}%p)")
    return "\n".join(lines)
