#!/usr/bin/env python3
"""
과거 predictions row를 action_type 기준으로 legacy 분류.

normalizer 도입(9b3bfeb) 이전 저장 row는 action_type/briefing_type이 비어
모순(IMMEDIATE+추격금지, signal=매도+홀딩전환)이 남아 있다. 이 스크립트는
과거 row를 **삭제하지 않고** action_normalizer의 결정론적 규칙으로 재분류해
legacy로 표시한다.

정책:
- normalizer_version이 'v1'이 아닌 row만 대상 (신규 v1 row는 건드리지 않음)
- original_signal 비면 현재 signal로 보존
- classify_row()로 action_type 산출
- CANCEL_SELL/HOLD_REVIEW면 signal='관망'으로 변경 (매도 모순 제거)
- normalizer_version='legacy' 표시
- reason 원문 수정 금지, predictions 삭제 금지

사용:
  python tools/migrate_prediction_action_types.py --dry-run   # 기본, 변경 없음
  python tools/migrate_prediction_action_types.py --apply     # 실제 적용
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DB_DIR  # noqa: E402
from core.action_normalizer import (  # noqa: E402
    CANCEL_SELL, HOLD_REVIEW, classify_row,
)

DB_PATH = DB_DIR / "memory.db"


def _is_held(ticker: str) -> bool:
    from config.settings import (
        HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION,
    )
    for h in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
        if ticker in h:
            return True
    return False


def migrate(apply: bool) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # normalizer_version 컬럼 보장
    cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
    if "normalizer_version" not in cols:
        conn.execute("ALTER TABLE predictions ADD COLUMN normalizer_version TEXT DEFAULT ''")
        conn.commit()

    rows = conn.execute(
        """SELECT id, signal, original_signal, reasoning, strategy_type, ticker, action_type, normalizer_version
           FROM predictions
           WHERE COALESCE(normalizer_version,'') != 'v1'"""
    ).fetchall()

    stats = {
        "scanned": len(rows), "updated": 0,
        "by_type": {}, "sell_to_watch": 0, "original_signal_filled": 0,
    }
    changes = []

    for r in rows:
        signal = r["signal"] or ""
        reason = r["reasoning"] or ""
        strat = r["strategy_type"] or ""
        ticker = r["ticker"] or ""

        atype = classify_row(signal, reason, strat, _is_held(ticker))
        new_signal = signal
        if atype in (CANCEL_SELL, HOLD_REVIEW):
            new_signal = "관망"
        new_original = r["original_signal"] or signal  # 비면 현재 signal 보존

        stats["by_type"][atype] = stats["by_type"].get(atype, 0) + 1
        if new_signal != signal:
            stats["sell_to_watch"] += 1
        if not r["original_signal"]:
            stats["original_signal_filled"] += 1
        stats["updated"] += 1

        changes.append((r["id"], atype, new_signal, new_original))

    if apply:
        for pid, atype, new_signal, new_original in changes:
            conn.execute(
                """UPDATE predictions
                   SET action_type=?, signal=?, original_signal=?, normalizer_version='legacy'
                   WHERE id=?""",
                (atype, new_signal, new_original, pid),
            )
        conn.commit()

    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="변경 없이 미리보기 (기본)")
    g.add_argument("--apply", action="store_true", help="실제 DB 적용")
    args = ap.parse_args()

    apply = args.apply
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== predictions action_type 마이그레이션 [{mode}] ===")
    if apply:
        print("⚠️ 실제 적용 — 사전 백업 필수 (db/backup/)")

    stats = migrate(apply)
    print(f"대상 row(비 v1): {stats['scanned']}")
    print(f"분류 처리: {stats['updated']}")
    print(f"  original_signal 보존: {stats['original_signal_filled']}건")
    print(f"  매도→관망(취소/홀딩): {stats['sell_to_watch']}건")
    print("  action_type 분포:")
    for k, v in sorted(stats["by_type"].items()):
        print(f"    {k}: {v}")
    if not apply:
        print("\n(dry-run — DB 변경 없음. 적용하려면 --apply)")


if __name__ == "__main__":
    main()
