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

    # ── legacy action_grade 모순 (명시) ──
    print()
    print("[legacy action_grade 모순 — migrate --apply로 보정]")
    legacy = "COALESCE(normalizer_version,'')='legacy'"
    cond_imm = _count(conn, f"{legacy} AND action_type='CONDITIONAL_NEW_BUY' AND action_grade='IMMEDIATE_ACTION'")
    cancel_imm = _count(conn, f"{legacy} AND action_type IN ('CANCEL_SELL','HOLD_REVIEW') AND action_grade='IMMEDIATE_ACTION'")
    watch_imm = _count(conn, f"{legacy} AND action_type='WATCH_ONLY' AND action_grade='IMMEDIATE_ACTION'")
    sell_hold = _count(conn, f"{legacy} AND signal='매도' AND (" +
                       " OR ".join("reasoning LIKE '%" + p + "%'" for p in SELL_CANCEL) + ")")
    print(f"  CONDITIONAL_NEW_BUY + IMMEDIATE: {cond_imm}")
    print(f"  CANCEL/HOLD + IMMEDIATE:         {cancel_imm}")
    print(f"  WATCH_ONLY + IMMEDIATE:          {watch_imm}")
    print(f"  signal=매도 + 홀딩/취소성 reason: {sell_hold}")
    legacy_grade_bad = cond_imm + cancel_imm + watch_imm + sell_hold
    print(f"  legacy 모순 총계: {'✅ 0건' if legacy_grade_bad == 0 else f'⚠️ {legacy_grade_bad}건 (migrate --apply 필요)'}")

    # ── 미분류(빈 normalizer_version) 잔존 ──
    print()
    unclassified = _count(conn, "COALESCE(normalizer_version,'')=''")
    print(f"[미분류(빈 normalizer_version): {unclassified}건]")
    if unclassified:
        print("  → migrate --apply로 legacy 분류 필요")

    conn.close()
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
