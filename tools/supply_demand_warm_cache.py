#!/usr/bin/env python3
"""tools/supply_demand_warm_cache.py — KRX 수급(외국인·기관) 배치 사전수집.

[배경]
quality gate의 score_supply_demand는 스코어 시점에 네이버 수급을 실시간
조회하는데 fetch_budget(3건) 제한 + 실패 시 무음 0.0 때문에 사실상 죽은
피처였다 (2026-07 실측: win/loss 양쪽 평균 0.0). 이 배치가 장 마감 후
유니버스 전체를 미리 수집해 파일 캐시(db/data/kr_frgn_cache.json)를 채우면,
스코어 시점엔 캐시 히트만 발생해 피처가 살아난다.

[안전]
- read-only 수집: 네이버 GET + 로컬 파일 캐시 쓰기만. 주문/브로커/DB 무관.
- 요청 간 간격 0.4s (네이버 예의) — 유니버스 ~60종목 기준 약 30초.

실행 (cron KST 15:40 평일):
    ./venv/bin/python tools/supply_demand_warm_cache.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KST = timezone(timedelta(hours=9))
_REQUEST_INTERVAL_SEC = 0.4


def _kr_universe() -> list[str]:
    """수집 대상 KR 종목코드(6자리) — 스캔 유니버스 + 포트폴리오 + 워치리스트."""
    codes: set[str] = set()
    try:
        from config.settings import SCAN_UNIVERSE_KR
        codes.update(SCAN_UNIVERSE_KR.keys())
    except Exception:
        pass
    try:
        from config.settings import PORTFOLIO, WATCHLIST
        for src in (PORTFOLIO, WATCHLIST):
            codes.update(src.keys())
    except Exception:
        pass
    out: list[str] = []
    for raw in codes:
        text = str(raw).upper().strip()
        if text.endswith((".KS", ".KQ")):
            text = text.split(".", 1)[0]
        if text.isdigit() and len(text) == 6:
            out.append(text)
    return sorted(set(out))


def main() -> int:
    from core.kr_market import _fetch_naver_frgn, _frgn_file_cache_path

    codes = _kr_universe()
    if not codes:
        print("WARM_CACHE_FAIL universe_empty")
        return 1

    ok, fail = 0, 0
    failed_codes: list[str] = []
    started = datetime.now(KST)
    for code in codes:
        rows = _fetch_naver_frgn(code, force_refresh=True)
        if rows:
            ok += 1
        else:
            fail += 1
            failed_codes.append(code)
        time.sleep(_REQUEST_INTERVAL_SEC)

    elapsed = (datetime.now(KST) - started).total_seconds()
    coverage = 100.0 * ok / len(codes)
    print(
        f"WARM_CACHE_DONE universe={len(codes)} ok={ok} fail={fail} "
        f"coverage={coverage:.1f}% elapsed={elapsed:.0f}s "
        f"cache={_frgn_file_cache_path()}"
    )
    if failed_codes:
        print("failed_codes=" + ",".join(failed_codes[:20]))
    # 커버리지 80% 미만이면 비정상 종료 — cron 로그에서 실패가 성공처럼 보이지 않게
    return 0 if coverage >= 80.0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
