"""
챗봇 대화 기록 + 매매 노트 저장소 (텔레그램 챗봇용)
Stock_bot/scripts/chat_db.py 이전
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from config.settings import DB_DIR, KST

DB_PATH = DB_DIR / "chat.db"
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_chat_db() -> None:
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conv_chat_time
            ON conversations(chat_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS trade_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            ticker TEXT,
            action TEXT,
            price TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trade_chat_time
            ON trade_notes(chat_id, created_at DESC);
    """)
    conn.commit()


def save_chat_message(chat_id: int, role: str, content: str) -> None:
    now = datetime.now(KST).isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conversations (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, now),
    )
    conn.commit()


def get_recent_chat(chat_id: int, limit: int = 20) -> list[str]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    result: list[str] = []
    for row in reversed(rows):
        prefix = "사용자" if row["role"] == "user" else "AI"
        result.append(f"{prefix}: {row['content']}")
    return result


def clear_chat(chat_id: int) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
    conn.commit()


def save_trade_note(chat_id: int, ticker: str, action: str, price: str, reason: str) -> None:
    now = datetime.now(KST).isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO trade_notes (chat_id, ticker, action, price, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, ticker, action, price, reason, now),
    )
    conn.commit()


def format_trade_notes(chat_id: int) -> str:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT ticker, action, price, reason, created_at FROM trade_notes WHERE chat_id = ? ORDER BY id DESC LIMIT 10",
        (chat_id,),
    ).fetchall()
    if not rows:
        return ""
    lines = ["━━━ 최근 매매 논의 이력 ━━━"]
    for n in reversed(rows):
        date = n["created_at"][:10]
        lines.append(f"  [{date}] {n['action']} {n['ticker']} @ {n['price']} — {n['reason']}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)
