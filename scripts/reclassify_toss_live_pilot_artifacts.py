"""scripts/reclassify_toss_live_pilot_artifacts.py

기존 toss live-pilot events DB에 쌓인 test/mock/리허설 live_sent row를
live_sent_artifact로 재분류한다 (production live_sent 오염 정리).

안전 원칙:
  - row 삭제 없음 (event_type만 재분류).
  - 실행 전 DB 백업 생성.
  - 진짜 live_sent(adapter_status='enabled' AND live_order_allowed=1)는 보존.
  - 그 외 live_sent(adapter disabled / not allowed)만 live_sent_artifact로 변경.

사용:
  python scripts/reclassify_toss_live_pilot_artifacts.py          # 적용
  python scripts/reclassify_toss_live_pilot_artifacts.py --dry-run # 미적용(미리보기)
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _db_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "toss_live_pilot_events.db"
    except Exception:
        return _ROOT / "db" / "data" / "toss_live_pilot_events.db"


# 진짜 live_sent 조건 — 이 조건이 아니면 artifact로 재분류
_NOT_REAL = (
    "event_type='live_sent' AND live_order_sent=1 "
    "AND NOT (adapter_status='enabled' AND live_order_allowed=1)"
)


def reclassify(dry_run: bool = False) -> dict:
    db = _db_path()
    if not db.exists():
        return {"ok": False, "reason": "db_not_found", "path": str(db)}

    conn = sqlite3.connect(str(db))
    try:
        target = conn.execute(
            f"SELECT COUNT(*) FROM live_pilot_events WHERE {_NOT_REAL}"
        ).fetchone()[0]
        real = conn.execute(
            "SELECT COUNT(*) FROM live_pilot_events "
            "WHERE event_type='live_sent' AND live_order_sent=1 "
            "AND adapter_status='enabled' AND live_order_allowed=1"
        ).fetchone()[0]

        if dry_run:
            return {
                "ok": True, "dry_run": True,
                "to_reclassify": target, "real_preserved": real,
            }

        # 백업 (삭제 없음 — 안전 복사본)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = db.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"toss_live_pilot_events_premigrate_{ts}.db"
        shutil.copy2(db, backup)

        conn.execute(
            f"UPDATE live_pilot_events SET event_type='live_sent_artifact' "
            f"WHERE {_NOT_REAL}"
        )
        conn.commit()
        return {
            "ok": True, "dry_run": False,
            "reclassified": target, "real_preserved": real,
            "backup": str(backup),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    result = reclassify(dry_run=dry)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
