"""
SQLite 데이터 저장소 — 매매 기록, 포지션, 예수금 관리
"""

from __future__ import annotations

import sqlite3

from config.settings import DB_DIR, DEFAULT_CASH
from core.models import PortfolioPosition, TradeRecord

DB_PATH = DB_DIR / "simulator.db"

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_tables(_conn)
    return _conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            ticker TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            shares INTEGER NOT NULL,
            avg_price REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            action TEXT NOT NULL,
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trades_time
            ON trades(created_at DESC);

        CREATE TABLE IF NOT EXISTS account (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        );
    """)
    # 예수금 초기값 설정 (없으면)
    existing = conn.execute(
        "SELECT value FROM account WHERE key = 'cash'"
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO account (key, value) VALUES ('cash', ?)",
            (DEFAULT_CASH,),
        )
    conn.commit()


# ─── 예수금 ──────────────────────────────────────────
def get_cash() -> float:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM account WHERE key = 'cash'").fetchone()
    return float(row["value"]) if row else DEFAULT_CASH


def save_cash(amount: float) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO account (key, value) VALUES ('cash', ?)",
        (amount,),
    )
    conn.commit()


# ─── 포지션 ──────────────────────────────────────────
def get_positions() -> list[PortfolioPosition]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM positions ORDER BY ticker").fetchall()
    return [
        PortfolioPosition(
            ticker=r["ticker"],
            name=r["name"],
            shares=r["shares"],
            avg_price=r["avg_price"],
        )
        for r in rows
    ]


def save_position(pos: PortfolioPosition) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO positions (ticker, name, shares, avg_price) "
        "VALUES (?, ?, ?, ?)",
        (pos.ticker, pos.name, pos.shares, pos.avg_price),
    )
    conn.commit()


def delete_position(ticker: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
    conn.commit()


# ─── 매매 기록 ───────────────────────────────────────
def save_trade(record: TradeRecord) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO trades (ticker, name, action, price, shares, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            record.ticker,
            record.name,
            record.action,
            record.price,
            record.shares,
            record.reason,
            record.created_at,
        ),
    )
    conn.commit()


def get_trades(limit: int = 20) -> list[TradeRecord]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        TradeRecord(
            id=r["id"],
            ticker=r["ticker"],
            name=r["name"],
            action=r["action"],
            price=r["price"],
            shares=r["shares"],
            reason=r["reason"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
