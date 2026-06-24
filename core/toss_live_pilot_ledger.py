"""core/toss_live_pilot_ledger.py

승인형 Live Pilot ledger — preview/confirm attempt 기록만.

스키마:
- previewed / cancelled / blocked / confirmed_but_not_sent
- 실제 체결/주문번호 필드 없음
- 민감정보 저장 금지

금지:
- 주문 체결 결과 저장
- accountNo/token/key 저장
- DELETE/UPDATE to change status to 'sent' or 'filled'
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 허용 status 값
_VALID_STATUSES = frozenset([
    "previewed", "payload_validated", "cancelled",
    "blocked", "confirmed_but_not_sent",
])

# DB 경로
def _db_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "toss_live_pilot.db"
    except Exception:
        return Path("db/data/toss_live_pilot.db")


_db_lock = threading.Lock()
_schema_created = False


def _conn() -> sqlite3.Connection:
    global _schema_created
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    if not _schema_created:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_pilot_ledger (
                pilot_id    TEXT PRIMARY KEY,
                preview_id  TEXT NOT NULL,
                symbol      TEXT NOT NULL,
                side        TEXT NOT NULL DEFAULT 'buy',
                quantity    INTEGER DEFAULT 0,
                limit_price REAL DEFAULT 0,
                estimated_amount_krw REAL DEFAULT 0,
                status      TEXT NOT NULL DEFAULT 'previewed',
                blocks      TEXT DEFAULT '[]',
                warnings    TEXT DEFAULT '[]',
                live_order_allowed  INTEGER NOT NULL DEFAULT 0,
                live_order_sent     INTEGER NOT NULL DEFAULT 0,
                adapter_status      TEXT DEFAULT 'disabled',
                created_at  TEXT NOT NULL,
                confirmed_at TEXT,
                cancelled_at TEXT,
                reason      TEXT DEFAULT ''
            )
        """)
        conn.commit()
        _schema_created = True
    return conn


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _gen_pilot_id() -> str:
    from datetime import datetime
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    import random
    seq = random.randint(1000, 9999)
    return f"tlive_{ts}_{seq}"


# ── 공개 API ───────────────────────────────────────────────────────

def record_live_pilot_preview(
    preview: dict,
    reason: str = "",
) -> dict:
    """Live Pilot 미리보기 ledger에 기록.

    status는 'previewed' 또는 'blocked' 중 하나.
    live_order_allowed=0, live_order_sent=0 고정.
    """
    symbol = preview.get("symbol", "")
    side = preview.get("side", "buy")
    quantity = int(preview.get("quantity", 0))
    limit_price = float(preview.get("limit_price", 0))
    estimated = float(preview.get("estimated_amount_krw", 0))
    blocks = preview.get("blocks", [])
    warnings = preview.get("warnings", [])
    status = "blocked" if not preview.get("ok", False) or blocks else "previewed"
    preview_id = preview.get("preview_id", _gen_pilot_id())
    pilot_id = _gen_pilot_id()

    row = (
        pilot_id, preview_id, symbol, side, quantity,
        limit_price, estimated, status,
        json.dumps(blocks, ensure_ascii=False),
        json.dumps(warnings, ensure_ascii=False),
        0, 0, "disabled", _now_kst(), None, None, reason,
    )

    with _db_lock:
        conn = _conn()
        try:
            conn.execute(
                """INSERT INTO live_pilot_ledger
                   (pilot_id, preview_id, symbol, side, quantity,
                    limit_price, estimated_amount_krw, status,
                    blocks, warnings, live_order_allowed, live_order_sent,
                    adapter_status, created_at, confirmed_at, cancelled_at, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            conn.commit()
        finally:
            conn.close()

    return {"ok": True, "pilot_id": pilot_id, "status": status}


def record_payload_validated(pilot_id: str) -> dict:
    """payload 검증 통과 상태로 업데이트. live_order_sent=0 유지."""
    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT status FROM live_pilot_ledger WHERE pilot_id=?", (pilot_id,)
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "pilot_id not found"}
            if existing["status"] != "previewed":
                return {"ok": False, "reason": f"cannot validate: status={existing['status']}"}
            conn.execute(
                "UPDATE live_pilot_ledger SET status='payload_validated' WHERE pilot_id=?",
                (pilot_id,),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "pilot_id": pilot_id, "status": "payload_validated", "live_order_sent": False}


def record_confirm_attempt(pilot_id: str) -> dict:
    """사용자가 2단계 승인을 시도했지만 adapter disabled로 차단됨."""
    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT status FROM live_pilot_ledger WHERE pilot_id=?", (pilot_id,)
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "pilot_id not found"}
            if existing["status"] not in ("previewed", "payload_validated"):
                return {"ok": False, "reason": f"cannot confirm: status={existing['status']}"}
            conn.execute(
                "UPDATE live_pilot_ledger SET status='confirmed_but_not_sent', "
                "confirmed_at=? WHERE pilot_id=?",
                (_now_kst(), pilot_id),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "pilot_id": pilot_id,
        "status": "confirmed_but_not_sent",
        "live_order_sent": False,
        "reason": "live_pilot_order_adapter_disabled",
    }


def cancel_live_pilot(pilot_id: str, reason: str = "user_cancelled") -> dict:
    """Live pilot 취소."""
    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT status FROM live_pilot_ledger WHERE pilot_id=?", (pilot_id,)
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "pilot_id not found"}
            if existing["status"] not in ("previewed",):
                return {"ok": False, "reason": f"cannot cancel: status={existing['status']}"}
            conn.execute(
                "UPDATE live_pilot_ledger SET status='cancelled', "
                "cancelled_at=? WHERE pilot_id=?",
                (_now_kst(), pilot_id),
            )
            conn.commit()
        finally:
            conn.close()

    return {"ok": True, "pilot_id": pilot_id, "status": "cancelled", "reason": reason}


def list_live_pilot_records(limit: int = 50) -> list[dict]:
    """최근 live pilot 기록 조회 (read-only)."""
    try:
        with _db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT * FROM live_pilot_ledger ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["blocks"] = json.loads(d.get("blocks") or "[]")
            d["warnings"] = json.loads(d.get("warnings") or "[]")
            result.append(d)
        return result
    except Exception as e:
        log.warning("list_live_pilot_records failed: %s", e)
        return []


def live_pilot_ledger_summary() -> dict:
    """Live pilot ledger 상태 요약 (read-only)."""
    try:
        with _db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM live_pilot_ledger GROUP BY status"
            ).fetchall()
            conn.close()
        counts = {r["status"]: r["cnt"] for r in rows}
        return {
            "counts": counts,
            "live_order_sent_total": 0,   # 항상 0, adapter disabled
            "adapter_status": "disabled",
            "live_order_allowed": False,
        }
    except Exception as e:
        log.warning("live_pilot_ledger_summary failed: %s", e)
        return {"counts": {}, "live_order_sent_total": 0, "adapter_status": "disabled"}
