"""
Toss paper trading ledger

가상 주문 기록 전용. 실제 Toss 주문 API 호출 없음.
별도 SQLite DB (db/data/toss_paper_trades.db)에만 저장.
기존 predictions/portfolio DB 변경 없음.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[1] / "db" / "data" / "toss_paper_trades.db"
_db_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    """paper trading DB 연결. 테이블 없으면 자동 생성."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'paper',
            side TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market TEXT DEFAULT '',
            quantity INTEGER DEFAULT 0,
            limit_price REAL DEFAULT 0,
            estimated_amount_krw REAL DEFAULT 0,
            reason TEXT DEFAULT '',
            confidence REAL DEFAULT 0,
            source_signal TEXT DEFAULT '',
            guard_status TEXT DEFAULT '',
            guard_reasons TEXT DEFAULT '[]',
            dry_run INTEGER NOT NULL DEFAULT 1,
            live_order_sent INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def record_paper_trade(
    side: str,
    symbol: str,
    quantity: int = 0,
    limit_price: float = 0,
    estimated_amount_krw: float = 0,
    market: str = "",
    reason: str = "",
    confidence: float = 0,
    source_signal: str = "",
    guard_status: str = "paper_filled",
    guard_reasons: list[str] | None = None,
) -> int:
    """paper trade 1건 기록. live_order_sent는 항상 false. 반환: row id."""
    now = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    reasons_json = json.dumps(guard_reasons or [])

    with _db_lock:
        conn = _conn()
        try:
            cur = conn.execute(
                """INSERT INTO paper_trades
                   (created_at, mode, side, symbol, market, quantity, limit_price,
                    estimated_amount_krw, reason, confidence, source_signal,
                    guard_status, guard_reasons, dry_run, live_order_sent)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,0)""",
                (now, "paper", side, symbol, market, quantity, limit_price,
                 estimated_amount_krw, reason, confidence, source_signal,
                 guard_status, reasons_json),
            )
            conn.commit()
            return cur.lastrowid or 0
        finally:
            conn.close()


def list_paper_trades(limit: int = 50, today_only: bool = False) -> list[dict]:
    """paper trade 목록 조회."""
    with _db_lock:
        conn = _conn()
        try:
            if today_only:
                today = datetime.now(KST).strftime("%Y-%m-%d")
                rows = conn.execute(
                    "SELECT * FROM paper_trades WHERE created_at LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"{today}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def today_paper_stats() -> dict:
    """오늘 paper trading 통계."""
    with _db_lock:
        conn = _conn()
        try:
            today = datetime.now(KST).strftime("%Y-%m-%d")
            row = conn.execute(
                """SELECT
                     COUNT(*) as count,
                     COALESCE(SUM(estimated_amount_krw), 0) as daily_amount_krw
                   FROM paper_trades
                   WHERE created_at LIKE ? AND guard_status IN ('paper_filled', 'allowed')""",
                (f"{today}%",),
            ).fetchone()
            return {
                "count": row["count"] if row else 0,
                "daily_amount_krw": row["daily_amount_krw"] if row else 0,
            }
        finally:
            conn.close()
