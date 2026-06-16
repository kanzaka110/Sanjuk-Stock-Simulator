#!/usr/bin/env python3
"""
predictions DB 무결성 감사.

전체/빈값/버전분포 + v1 기준 모순 count를 출력한다.
v1(normalizer 경유) row는 모순 0건이 정상. legacy/과거 row의 모순은 참고용.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DB_DIR  # noqa: E402

DB_PATH = DB_DIR / "memory.db"

BUY_BLOCK = ("추격 금지", "조건 미충족", "FOMC 후", "눌림목", "대기")
SELL_CANCEL = ("매도 취소", "홀딩 전환", "홀딩 유지", "잔여 보유")


def _count(conn, where, params=()):
    return conn.execute(f"SELECT COUNT(*) FROM predictions WHERE {where}", params).fetchone()[0]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total = _count(conn, "1=1")
    print("=== predictions 무결성 감사 ===")
    print(f"전체 row: {total}")
    print()
    blank = "COALESCE({col},'')=''"
    at_blank = _count(conn, blank.format(col="action_type"))
    bt_blank = _count(conn, blank.format(col="briefing_type"))
    os_blank = _count(conn, blank.format(col="original_signal"))
    print("[빈 값]")
    print(f"  action_type blank:     {at_blank}")
    print(f"  briefing_type blank:   {bt_blank}")
    print(f"  original_signal blank: {os_blank}")
    print()

    print("[normalizer_version 분포]")
    for r in conn.execute(
        "SELECT COALESCE(normalizer_version,'') v, COUNT(*) c FROM predictions GROUP BY v ORDER BY c DESC"
    ).fetchall():
        label = r["v"] or "(빈값=미분류)"
        print(f"  {label}: {r['c']}")
    print()

    # ── v1 기준 모순 (0이어야 정상) ──
    print("[v1 기준 모순 count — 0이어야 정상]")
    v1 = "COALESCE(normalizer_version,'')='v1'"
    bad = 0
    for p in BUY_BLOCK:
        n = _count(conn, f"{v1} AND action_grade='IMMEDIATE_ACTION' AND reasoning LIKE ?", (f"%{p}%",))
        bad += n
        print(f"  IMMEDIATE + '{p}': {n}")
    for p in SELL_CANCEL:
        n = _count(conn, f"{v1} AND signal='매도' AND reasoning LIKE ?", (f"%{p}%",))
        bad += n
        print(f"  signal=매도 + '{p}': {n}")
    n = _count(conn, f"{v1} AND action_type='CONDITIONAL_NEW_BUY' AND action_grade='IMMEDIATE_ACTION'")
    bad += n
    print(f"  CONDITIONAL_NEW_BUY + IMMEDIATE: {n}")
    n = _count(conn, f"{v1} AND action_type IN ('CANCEL_SELL','HOLD_REVIEW') AND signal='매도'")
    bad += n
    print(f"  CANCEL/HOLD + signal=매도: {n}")
    print()
    print(f"v1 모순 총계: {'✅ 0건' if bad == 0 else f'⚠️ {bad}건'}")

    # ── 참고: legacy/과거 모순 ──
    print()
    print("[참고: 비-v1(legacy/과거) 모순 — 마이그레이션 대상]")
    non_v1 = "COALESCE(normalizer_version,'')!='v1'"
    legacy_bad = 0
    for p in BUY_BLOCK[:3]:
        legacy_bad += _count(conn, f"{non_v1} AND action_grade='IMMEDIATE_ACTION' AND reasoning LIKE ?", (f"%{p}%",))
    for p in SELL_CANCEL[:2]:
        legacy_bad += _count(conn, f"{non_v1} AND signal='매도' AND reasoning LIKE ?", (f"%{p}%",))
    print(f"  비-v1 모순(주요): {legacy_bad}건 (migrate --apply로 legacy 분리 시 해소)")

    conn.close()
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
