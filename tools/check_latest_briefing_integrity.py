#!/usr/bin/env python3
"""
최근 브리핑 무결성 확인 — 실브리핑이 v1 normalizer 경로를 정상 통과했는지.

가장 최근 created_at의 브리핑(같은 날) 기준으로 action_type/signal/grade 분포와
조건부 매수·매도 취소 수, 모순 count, 최근 row 10개를 출력한다.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DB_DIR  # noqa: E402

DB_PATH = DB_DIR / "memory.db"

BUY_BLOCK = ("추격 금지", "조건 미충족", "FOMC 후", "눌림목", "대기")
SELL_CANCEL = ("매도 취소", "홀딩 전환", "홀딩 유지", "잔여 보유")
# 상단 판단 보류 표현 — IMMEDIATE 매수와 충돌
EVENT_WAIT = ("오늘 실행 없음", "신규 진입 보류", "FOMC 대기", "확인 후 진입",
              "이벤트 대기", "진입 보류", "매수 보류")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    latest = conn.execute("SELECT MAX(created_at) m FROM predictions").fetchone()["m"]
    if not latest:
        print("predictions 비어 있음")
        return 0
    day = latest[:10]
    rows = conn.execute(
        "SELECT * FROM predictions WHERE created_at LIKE ? ORDER BY created_at DESC",
        (f"{day}%",),
    ).fetchall()

    print(f"=== 최근 브리핑 무결성 ({day}) ===")
    bt = {r["briefing_type"] or "(빈값)" for r in rows}
    print(f"latest created_at: {latest}")
    print(f"briefing_type: {', '.join(sorted(bt))}")
    print(f"row 수: {len(rows)}")
    print()

    def _dist(field):
        d = {}
        for r in rows:
            k = r[field] or "(빈값)"
            d[k] = d.get(k, 0) + 1
        return d

    for field in ("action_type", "signal", "action_grade", "normalizer_version"):
        print(f"[{field} 분포]")
        for k, v in sorted(_dist(field).items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")
        print()

    cond = sum(1 for r in rows if r["action_type"] == "CONDITIONAL_NEW_BUY")
    cancel = sum(1 for r in rows if r["action_type"] in ("CANCEL_SELL", "HOLD_REVIEW"))
    print(f"조건부 매수: {cond} / 매도 취소·홀딩: {cancel}")
    print()

    # 모순 (v1만)
    bad = 0
    conflict = 0
    for r in rows:
        if (r["normalizer_version"] or "") != "v1":
            continue
        reason = r["reasoning"] or ""
        if r["action_grade"] == "IMMEDIATE_ACTION" and any(p in reason for p in BUY_BLOCK):
            bad += 1
        if r["signal"] == "매도" and any(p in reason for p in SELL_CANCEL):
            bad += 1
        # 상단판단 충돌: 즉시실행 매수인데 보류성 reason
        if (r["signal"] == "매수" and r["action_grade"] == "IMMEDIATE_ACTION"
                and any(p in reason for p in EVENT_WAIT)):
            conflict += 1
    print(f"v1 모순: {'✅ 0건' if bad == 0 else f'⚠️ {bad}건'}")
    print(f"상단판단 충돌(즉시매수+보류성 reason): {'✅ 0건' if conflict == 0 else f'⚠️ {conflict}건'}")
    print()

    print("[최근 row 10개]")
    for r in rows[:10]:
        print(f"  {r['created_at'][11:16]} {r['name'][:12]:12s} "
              f"{r['signal']}/{r['action_type'] or '-'} "
              f"grade={r['action_grade'] or '-'} bt={r['briefing_type'] or '-'} "
              f"ver={r['normalizer_version'] or '-'}")

    conn.close()
    return 0 if (bad == 0 and conflict == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
