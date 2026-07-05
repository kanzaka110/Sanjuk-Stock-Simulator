"""core/source_health.py

데이터 소스 헬스체크 — 주간 리포트용.

전 소스가 fail-safe(실패 시 조용히 스킵) 설계라 어느 날 원천이 막혀도
브리핑은 그냥 얇아질 뿐 티가 안 남 → 주 1회 캐시/상태 파일 신선도로 감지.

라이브 호출 없음 (파일 mtime/타임스탬프만 확인) — 비용 $0, 실패 불가.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _data_dir() -> Path:
    try:
        from db.store import DB_DIR
        return Path(DB_DIR)
    except Exception:
        return Path("db/data")


def _age_hours_from_epoch(saved_at: float) -> float | None:
    if not saved_at:
        return None
    return (time.time() - saved_at) / 3600


def _age_hours_from_iso(iso: str) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return (datetime.now(KST) - dt).total_seconds() / 3600
    except ValueError:
        return None


def _load(name: str) -> dict:
    p = _data_dir() / name
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("health load failed (%s): %s", name, e)
    return {}


def _fmt(label: str, age_h: float | None, warn_h: float) -> str:
    """age가 warn_h 초과면 ⚠️, 파일 자체가 없으면 ❌."""
    if age_h is None:
        return f"  ❌ {label}: 데이터 없음 (한 번도 성공 못 함)"
    if age_h > warn_h:
        return f"  ⚠️ {label}: {age_h:.0f}시간 전 (기대 {warn_h:.0f}h 이내) — 원천 차단/오류 의심"
    unit = f"{age_h * 60:.0f}분 전" if age_h < 1 else f"{age_h:.0f}시간 전"
    return f"  ✅ {label}: {unit}"


def source_health_report() -> str:
    """소스별 최근 성공 시각 요약. 주간 리포트 섹션용."""
    lines: list[str] = []

    # FRED — 브리핑마다 갱신 (6h 캐시) → 하루 4회 브리핑 기준 30h 넘으면 이상
    fred = _load("fred_cache.json")
    lines.append(_fmt("FRED 매크로", _age_hours_from_epoch(fred.get("saved_at", 0)), 30))

    # Fear & Greed — 3h 캐시, 브리핑마다 갱신
    fg = _load("fear_greed_cache.json")
    lines.append(_fmt("CNN Fear&Greed", _age_hours_from_epoch(fg.get("saved_at", 0)), 30))

    # EDGAR — monitor 루프 60분 스로틀 (일요일 스킵) → 48h 넘으면 이상
    edgar = _load("edgar_monitor_state.json")
    lines.append(_fmt("SEC EDGAR", _age_hours_from_iso(edgar.get("last_checked_at", "")), 48))

    # DART — monitor 루프 30분 스로틀
    dart = _load("dart_monitor_state.json")
    dart_age = _age_hours_from_iso(dart.get("last_checked_at", ""))
    if dart_age is None:  # 구버전 state 포맷 대비 — 파일 mtime 폴백
        p = _data_dir() / "dart_monitor_state.json"
        dart_age = (time.time() - p.stat().st_mtime) / 3600 if p.exists() else None
    lines.append(_fmt("DART 공시", dart_age, 48))

    # 실적 알림 — 12h 스로틀 (주간에만)
    ea = _load("earnings_alert_state.json")
    lines.append(_fmt("실적 D-1 알림", _age_hours_from_iso(ea.get("last_checked_at", "")), 48))

    # KIS 토큰 — 24h 갱신, 없으면 시세/공매도 전부 yfinance 폴백 중
    # (token 원문 노출 방지 — 파일 내용은 읽지 않고 mtime만 확인)
    p = _data_dir() / "kis_token.json"
    kis_age = (time.time() - p.stat().st_mtime) / 3600 if p.exists() else None
    lines.append(_fmt("KIS 토큰 (시세·공매도)", kis_age, 48))

    return "\n".join(lines)
