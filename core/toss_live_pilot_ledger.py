"""core/toss_live_pilot_ledger.py

승인형 Live Pilot ledger.

스키마:
- previewed / reviewed / payload_validated / cancelled / blocked
- confirmed_but_not_sent / live_send_blocked / live_sent / live_send_failed
- 민감정보 저장 금지 (accountNo/token/key/secret)

금지:
- accountNo/token/key 저장
- live_order_sent를 transport 없이 True로 저장
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
    "previewed", "reviewed", "payload_validated", "cancelled", "blocked",
    "confirmed_but_not_sent", "live_send_blocked", "live_sent", "live_send_failed",
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
                broker_order_id     TEXT DEFAULT '',
                failure_reason      TEXT DEFAULT '',
                payload_hash        TEXT DEFAULT '',
                sent_at             TEXT,
                created_at  TEXT NOT NULL,
                confirmed_at TEXT,
                cancelled_at TEXT,
                reason      TEXT DEFAULT ''
            )
        """)
        # 기존 DB에 새 컬럼 추가 (이미 있으면 무시)
        for col, defn in [
            ("broker_order_id", "TEXT DEFAULT ''"),
            ("failure_reason",  "TEXT DEFAULT ''"),
            ("payload_hash",    "TEXT DEFAULT ''"),
            ("sent_at",         "TEXT"),
            ("stop_loss",       "TEXT"),
            ("invalidation",    "TEXT"),
            ("target_price",    "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE live_pilot_ledger ADD COLUMN {col} {defn}")
            except Exception:
                pass
        conn.commit()
        _schema_created = True
    return conn


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _gen_pilot_id() -> str:
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

    status: 'previewed' 또는 'blocked'.
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

    stop_loss = preview.get("stop_loss") or ""
    invalidation = preview.get("invalidation") or ""
    target_price = preview.get("target_price") or ""
    # stop_loss가 숫자면 문자열로 변환
    if isinstance(stop_loss, (int, float)):
        stop_loss = str(stop_loss)
    if isinstance(target_price, (int, float)):
        target_price = str(target_price)

    row = (
        pilot_id, preview_id, symbol, side, quantity,
        limit_price, estimated, status,
        json.dumps(blocks, ensure_ascii=False),
        json.dumps(warnings, ensure_ascii=False),
        0, 0, "disabled", "", "", "", _now_kst(), None, None, reason,
        stop_loss, invalidation, target_price,
    )

    with _db_lock:
        conn = _conn()
        try:
            conn.execute(
                """INSERT INTO live_pilot_ledger
                   (pilot_id, preview_id, symbol, side, quantity,
                    limit_price, estimated_amount_krw, status,
                    blocks, warnings, live_order_allowed, live_order_sent,
                    adapter_status, broker_order_id, failure_reason, payload_hash,
                    created_at, confirmed_at, cancelled_at, reason,
                    stop_loss, invalidation, target_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            conn.commit()
        finally:
            conn.close()

    return {"ok": True, "pilot_id": pilot_id, "status": status}


def record_reviewed(pilot_id: str) -> dict:
    """Telegram 검토 완료 상태로 업데이트. live_order_sent=0 유지."""
    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT status FROM live_pilot_ledger WHERE pilot_id=?", (pilot_id,)
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "pilot_id not found"}
            if existing["status"] not in ("previewed", "payload_validated"):
                return {"ok": False, "reason": f"cannot review: status={existing['status']}"}
            conn.execute(
                "UPDATE live_pilot_ledger SET status='reviewed' WHERE pilot_id=?",
                (pilot_id,),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "pilot_id": pilot_id, "status": "reviewed", "live_order_sent": False}


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
            if existing["status"] not in ("previewed", "reviewed", "payload_validated"):
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


def record_live_send_blocked(pilot_id: str, reasons: list[str]) -> dict:
    """can_send_live_pilot_order 실패 — 주문 조건 미충족으로 차단."""
    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT status FROM live_pilot_ledger WHERE pilot_id=?", (pilot_id,)
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "pilot_id not found"}
            conn.execute(
                "UPDATE live_pilot_ledger SET status='live_send_blocked', "
                "failure_reason=? WHERE pilot_id=?",
                (json.dumps(reasons, ensure_ascii=False), pilot_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "ok": True, "pilot_id": pilot_id,
        "status": "live_send_blocked", "live_order_sent": False,
    }


def record_live_sent(
    pilot_id: str,
    broker_order_id: str = "",
    payload_hash: str = "",
) -> dict:
    """실제 주문 전송 성공 기록.

    live_order_sent=1 — transport가 성공 응답 시에만 호출.
    민감정보(accountNo/token) 저장 금지.
    """
    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT status FROM live_pilot_ledger WHERE pilot_id=?", (pilot_id,)
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "pilot_id not found"}
            conn.execute(
                "UPDATE live_pilot_ledger SET status='live_sent', "
                "live_order_sent=1, broker_order_id=?, payload_hash=?, sent_at=? "
                "WHERE pilot_id=?",
                (broker_order_id, payload_hash, _now_kst(), pilot_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "ok": True, "pilot_id": pilot_id,
        "status": "live_sent", "live_order_sent": True,
    }


def record_live_send_failed(
    pilot_id: str,
    failure_reason: str = "",
    payload_hash: str = "",
) -> dict:
    """실제 주문 전송 실패 기록. failure_reason 빈칸 금지 (진단 의무화)."""
    failure_reason = (failure_reason or "").strip() or "unspecified_failure"
    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT status FROM live_pilot_ledger WHERE pilot_id=?", (pilot_id,)
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "pilot_id not found"}
            conn.execute(
                "UPDATE live_pilot_ledger SET status='live_send_failed', "
                "live_order_sent=0, failure_reason=?, payload_hash=? WHERE pilot_id=?",
                (failure_reason, payload_hash, pilot_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "ok": True, "pilot_id": pilot_id,
        "status": "live_send_failed", "live_order_sent": False,
    }


def record_live_send_retryable(
    pilot_id: str,
    failure_reason: str = "",
    payload_hash: str = "",
) -> dict:
    """일시적 전송 실패 기록. terminal failed로 소비하지 않고 보류 상태로 남긴다.

    failure_reason 빈칸 금지 (진단 의무화).
    """
    failure_reason = (failure_reason or "").strip() or "unspecified_failure"
    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT status FROM live_pilot_ledger WHERE pilot_id=?", (pilot_id,)
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "pilot_id not found"}
            conn.execute(
                "UPDATE live_pilot_ledger SET status='live_send_retryable', "
                "live_order_sent=0, failure_reason=?, payload_hash=? WHERE pilot_id=?",
                (failure_reason, payload_hash, pilot_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "ok": True, "pilot_id": pilot_id,
        "status": "live_send_retryable", "live_order_sent": False,
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
            if existing["status"] not in ("previewed", "reviewed", "payload_validated"):
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
            live_sent_total = conn.execute(
                "SELECT COALESCE(SUM(live_order_sent), 0) FROM live_pilot_ledger"
            ).fetchone()[0]
            conn.close()
        counts = {r["status"]: r["cnt"] for r in rows}
        return {
            "counts": counts,
            "live_order_sent_total": int(live_sent_total),
            "adapter_status": "disabled",   # 코드 기본값
            "live_order_allowed": False,
        }
    except Exception as e:
        log.warning("live_pilot_ledger_summary failed: %s", e)
        return {"counts": {}, "live_order_sent_total": 0, "adapter_status": "disabled"}
