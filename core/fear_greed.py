"""
CNN Fear & Greed Index — 시장 심리 보조 지표

비공식 엔드포인트라 언제든 깨질 수 있음 → 실패 시 빈 dict (레짐 판정에 미사용,
브리핑 컨텍스트 보조 텍스트로만 주입). requests TLS/봇 차단 → curl + Referer 사용.
3시간 파일 캐시.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CACHE_FILE = Path(__file__).resolve().parent.parent / "db" / "data" / "fear_greed_cache.json"
_CACHE_TTL_SEC = 3 * 3600

_RATING_KR = {
    "extreme fear": "극도의 공포",
    "fear": "공포",
    "neutral": "중립",
    "greed": "탐욕",
    "extreme greed": "극도의 탐욕",
}


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text())
            if time.time() - data.get("saved_at", 0) < _CACHE_TTL_SEC:
                return data.get("snapshot", {})
    except Exception as e:
        log.warning("F&G 캐시 읽기 실패: %s", e)
    return {}


def _save_cache(snapshot: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({"saved_at": time.time(), "snapshot": snapshot}))
    except Exception as e:
        log.warning("F&G 캐시 저장 실패: %s", e)


def fetch_fear_greed() -> dict:
    """CNN Fear & Greed 스냅샷. 실패 시 {}.

    Returns:
        {"score": 31.9, "rating": "fear", "rating_kr": "공포",
         "prev_close": ..., "prev_week": ..., "prev_month": ..., "as_of": "..."}
    """
    cached = _load_cache()
    if cached:
        return cached

    try:
        proc = subprocess.run(
            [
                "curl", "-sS", "--max-time", "15",
                "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "-H", "Accept: application/json",
                "-H", "Referer: https://edition.cnn.com/markets/fear-and-greed",
                _URL,
            ],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            log.warning("F&G curl 실패: %s", proc.stderr.strip()[:200])
            return {}
        fg = json.loads(proc.stdout).get("fear_and_greed") or {}
        score = fg.get("score")
        if score is None:
            return {}
        rating = str(fg.get("rating", "")).lower()
        snapshot = {
            "score": round(float(score), 1),
            "rating": rating,
            "rating_kr": _RATING_KR.get(rating, rating),
            "prev_close": round(float(fg.get("previous_close", 0)), 1),
            "prev_week": round(float(fg.get("previous_1_week", 0)), 1),
            "prev_month": round(float(fg.get("previous_1_month", 0)), 1),
            "as_of": str(fg.get("timestamp", ""))[:16],
        }
        _save_cache(snapshot)
        return snapshot
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        log.warning("F&G 조회 실패: %s", e)
        return {}


def fear_greed_to_text(snap: dict) -> str:
    """F&G 스냅샷 → 브리핑 보조 텍스트. 빈 스냅샷이면 빈 문자열."""
    if not snap:
        return ""
    chg_w = snap["score"] - snap.get("prev_week", snap["score"])
    chg_m = snap["score"] - snap.get("prev_month", snap["score"])
    return (
        f"【CNN Fear & Greed】 {snap['score']:.0f}/100 [{snap['rating_kr']}] "
        f"(1주 {chg_w:+.0f} | 1개월 {chg_m:+.0f})"
    )
