"""core/toss_live_pilot_events.py

Live pilot callback 이벤트 로그.

버튼 callback(review/cancel/confirm)의 결과를 DB에 기록해
Hermes가 GET polling으로 확인할 수 있게 한다.

event_type:
  preview_created          | 미리보기 생성
  reviewed                 | 검토 완료
  cancelled                | 취소
  confirm_attempted        | 최종 승인 시도
  confirm_blocked_hermes   | Hermes 검증 미완료/미통과로 차단
  confirm_blocked_policy   | Hermes PASS + live pilot 조건 미충족으로 차단
  confirm_blocked_transport| Hermes PASS + transport 미설정으로 차단
  confirmed_but_not_sent   | Hermes PASS + guard 통과 + 최종 차단 (adapter 등)
  live_send_blocked        | guard 차단
  live_sent                | 실제/fake 전송 성공
  live_send_failed         | 전송 시도 후 실패

금지:
  - row 삭제 없음
  - 민감정보 없음 (accountNo/token/key/secret)
  - broker raw response 저장 금지
  - live_order_allowed 변경 금지
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core.toss_live_pilot_hermes_bridge import SYMBOL_NAMES, get_symbol_display

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_VALID_EVENT_TYPES = frozenset([
    "preview_created",
    "reviewed",
    "cancelled",
    "confirm_attempted",
    "confirm_blocked_hermes",
    "confirm_blocked_policy",
    "confirm_blocked_transport",
    "confirmed_but_not_sent",
    "live_send_blocked",
    "live_sent",
    "live_send_failed",
])


def _db_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "toss_live_pilot_events.db"
    except Exception:
        return Path("db/data/toss_live_pilot_events.db")


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
            CREATE TABLE IF NOT EXISTS live_pilot_events (
                event_id            TEXT PRIMARY KEY,
                pilot_id            TEXT NOT NULL,
                preview_id          TEXT DEFAULT '',
                verification_id     TEXT DEFAULT '',
                event_type          TEXT NOT NULL,
                status              TEXT NOT NULL,
                symbol              TEXT DEFAULT '',
                symbol_name         TEXT DEFAULT '',
                symbol_label        TEXT DEFAULT '',
                side                TEXT DEFAULT 'buy',
                quantity            INTEGER DEFAULT 0,
                limit_price         REAL DEFAULT 0,
                estimated_amount_krw REAL DEFAULT 0,
                live_order_sent     INTEGER DEFAULT 0,
                adapter_status      TEXT DEFAULT 'disabled',
                live_order_allowed  INTEGER DEFAULT 0,
                reason              TEXT DEFAULT '',
                message             TEXT DEFAULT '',
                created_at          TEXT NOT NULL,
                delivered_to_hermes INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        _schema_created = True
    return conn


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _gen_event_id() -> str:
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    seq = random.randint(1000, 9999)
    return f"tle_{ts}_{seq}"


# ─── 이벤트 기록 ─────────────────────────────────────────

def record_event(
    pilot_id: str,
    event_type: str,
    status: str,
    *,
    preview_id: str = "",
    verification_id: str = "",
    symbol: str = "",
    side: str = "buy",
    quantity: int = 0,
    limit_price: float = 0.0,
    estimated_amount_krw: float = 0.0,
    live_order_sent: bool = False,
    adapter_status: str = "disabled",
    reason: str = "",
    message: str = "",
) -> dict:
    """Live pilot 이벤트 기록.

    민감정보(accountNo/token/key/secret/broker raw) 포함 금지.
    live_order_allowed 항상 0.

    Returns:
        {"ok": bool, "event_id": str, "event_type": str}
    """
    if event_type not in _VALID_EVENT_TYPES:
        log.warning("invalid event_type: %s", event_type)
        return {"ok": False, "reason": f"invalid event_type: {event_type!r}"}

    # 민감정보 가드
    for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
        for field in (reason, message):
            if kw in str(field):
                log.warning("민감정보 감지 in event fields: %s", kw)
                return {"ok": False, "reason": f"sensitive_field: {kw}"}

    event_id = _gen_event_id()
    symbol_name = SYMBOL_NAMES.get(symbol, "")
    symbol_label = get_symbol_display(symbol)
    now = _now_kst()

    try:
        with _db_lock:
            conn = _conn()
            conn.execute(
                """INSERT INTO live_pilot_events
                   (event_id, pilot_id, preview_id, verification_id,
                    event_type, status, symbol, symbol_name, symbol_label,
                    side, quantity, limit_price, estimated_amount_krw,
                    live_order_sent, adapter_status, live_order_allowed,
                    reason, message, created_at, delivered_to_hermes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, pilot_id, preview_id, verification_id,
                    event_type, status, symbol, symbol_name, symbol_label,
                    side, quantity, limit_price, estimated_amount_krw,
                    1 if live_order_sent else 0, adapter_status, 0,
                    reason, message, now, 0,
                ),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        log.warning("record_event failed: %s", e)
        return {"ok": False, "reason": str(e)}

    return {
        "ok": True,
        "event_id": event_id,
        "event_type": event_type,
        "status": status,
        "pilot_id": pilot_id,
        "symbol_label": symbol_label,
        "live_order_sent": live_order_sent,
        "live_order_allowed": False,
        "created_at": now,
    }


# ─── 조회 ────────────────────────────────────────────────

def list_events(limit: int = 50) -> list[dict]:
    """최근 이벤트 조회 (최신순, read-only)."""
    try:
        with _db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT * FROM live_pilot_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["live_order_sent"] = bool(d.get("live_order_sent"))
            d["live_order_allowed"] = False  # 항상 false
            result.append(d)
        return result
    except Exception as e:
        log.warning("list_events failed: %s", e)
        return []


def event_summary() -> dict:
    """이벤트 타입별 건수 요약 (read-only)."""
    try:
        with _db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT event_type, COUNT(*) as cnt FROM live_pilot_events GROUP BY event_type"
            ).fetchall()
            live_sent_count = conn.execute(
                "SELECT COUNT(*) FROM live_pilot_events WHERE live_order_sent=1"
            ).fetchone()[0]
            conn.close()
        counts = {r["event_type"]: r["cnt"] for r in rows}
        return {
            "summary": counts,
            "live_order_sent_total": live_sent_count,
            "live_order_allowed": False,
        }
    except Exception as e:
        log.warning("event_summary failed: %s", e)
        return {"summary": {}, "live_order_sent_total": 0, "live_order_allowed": False}
